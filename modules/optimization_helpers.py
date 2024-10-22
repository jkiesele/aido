import torch
import numpy as np
from typing import Literal


class OneHotEncoder(torch.nn.Module):
    """
    OneHotEncoder is a module that performs one-hot encoding on discrete values.

    Attributes:
        logits (torch.Tensor): A set of unnormalized, real-valued scores for each category. These logits
            represent the model's confidence in each category prior to normalization. They can take any
            real value, including negatives, and are not probabilities themselves. Use the to_probabilities()
            method to convert the logits to probabilities.
    """
    def __init__(self, parameter: dict, temperature: float = 1.0):
        """
        Args:
            parameter (dict): A dictionary containing the parameter information.
            temperature (float, optional): The temperature parameter for gumbel softmax. Defaults to 1.0.
        """
        super().__init__()
        self.discrete_values: list = parameter["discrete_values"]
        self.starting_value = torch.tensor(self.discrete_values.index(parameter["current_value"]))
        self.logits = torch.nn.Parameter(
            torch.tensor(np.repeat(1 / len(self.discrete_values), len(self.discrete_values)), dtype=torch.float32),
            requires_grad=True
        )
        self.temperature = temperature
        self._cost: list = parameter["cost"]

    def forward(self):
        return torch.nn.functional.gumbel_softmax(self.logits, tau=self.temperature, hard=True)

    @property
    def current_value(self):
        """ Returns the index corresponding to an entry in 'discrete_values'
        """
        return torch.argmax(self.logits.clone().detach())

    @property
    def physical_value(self):
        return self.discrete_values[self.current_value.item()]

    @property
    def probabilities(self):
        return torch.nn.functional.softmax(self.logits, dim=0)

    @property
    def cost(self):
        return self._cost[self.current_value.item()]


class ContinuousParameter(torch.nn.Module):
    def __init__(self, parameter: dict):
        super().__init__()
        self.starting_value = torch.tensor(parameter["current_value"])
        self.parameter = torch.nn.Parameter(self.starting_value.clone(), requires_grad=True)
        self.min_value = np.nan_to_num(parameter["min_value"], nan=-10E10)
        self.max_value = np.nan_to_num(parameter["max_value"], nan=10E10)
        self.boundaries = torch.tensor(np.array([self.min_value, self.max_value], dtype="float32"))
        self.sigma = np.array(parameter["sigma"])
        self._cost = parameter["cost"]

    def forward(self):
        return torch.unsqueeze(self.parameter, 0)

    @property
    def current_value(self):  # TODO Normalization
        return self.parameter
    
    @property
    def physical_value(self):
        return self.current_value.item()  # TODO Keep without normalization
    
    @property
    def cost(self):
        return self.physical_value * self._cost
    

class ParameterModule(torch.nn.ModuleDict):
    def __init__(self, parameter_dict: dict[str, dict]):
        self.parameter_dict = parameter_dict
        self.parameters_discrete: dict[str, OneHotEncoder] = {}
        self.parameters_continuous: dict[str, ContinuousParameter] = {}
        self.covariance = self.reset_covariance()
        super().__init__()

        for name, parameter in self.parameter_dict.items():
            if parameter.get("discrete_values"):
                self.parameters_discrete[name] = OneHotEncoder(parameter, temperature=0.2)
                self[name] = self.parameters_discrete[name]
            else:
                self.parameters_continuous[name] = ContinuousParameter(parameter)
                self[name] = self.parameters_continuous[name]

    def reset_covariance(self):
        return np.diag(np.array(
            [parameter.sigma.item() for parameter in self.parameters_continuous.values()],
            dtype="float32"
        ))

    def forward(self):
        tensor_list = [parameter() for parameter in self.values()]
        return torch.concat(tensor_list)

    @property
    def discrete(self):
        return super().__init__(self.parameters_discrete)
    
    @property
    def continuous(self):
        return super().__init__(self.parameters_continuous)

    def tensor(self, parameter_types: Literal["all", "discrete", "continuous"] = "all"):
        types = {
            "all": self,
            "discrete": self.parameters_discrete,
            "continuous": self.parameters_continuous
        }
        tensor_list = [parameter.current_value for parameter in types[parameter_types].values()]
        if tensor_list == []:
            return torch.tensor([])
        else:
            return torch.stack(tensor_list)
        
    @property
    def physical_values(self) -> list:
        return [parameter.physical_value for parameter in self.values()]

    @property
    def constraints(self) -> torch.Tensor:
        tensor_list = [parameter.boundaries for parameter in self.parameters_continuous.values()]
        if tensor_list == []:
            return torch.tensor([])
        else:
            return torch.stack(tensor_list)

    @property
    def cost_loss(self) -> torch.Tensor:
        return sum(parameter.cost for parameter in self.values())
    
    def adjust_covariance(self, direction: torch.Tensor, min_scale=2.0):
        """ Stretches the box_covariance of the generator in the directon specified as input.
        Direction is a vector in parameter space
        """
        parameter_direction_vector = direction.detach().cpu().numpy()
        parameter_direction_length = np.linalg.norm(parameter_direction_vector)

        scaling_factor = min_scale * np.max([1., 4. * parameter_direction_length])
        # Create the scaling adjustment matrix
        parameter_direction_normed = parameter_direction_vector / parameter_direction_length
        M_scaled = (scaling_factor - 1) * np.outer(parameter_direction_normed, parameter_direction_normed)
        # Adjust the original covariance matrix
        self.covariance = np.diag(self.covariance**2) + M_scaled
        return np.diag(self.covariance)

    def check_parameters_are_local(self, updated_parameters: torch.Tensor, scale=1.0) -> bool:
        """ Assure that the predicted parameters by the optimizer are within the bounds of the covariance
        matrix spanned by the 'sigma' of each parameter.
        """
        diff = updated_parameters - self.tensor("continuous")
        diff = diff.detach().cpu().numpy()

        if np.any(self.covariance >= 10E3) or not np.any(self.covariance):
            self.covariance = self.reset_covariance()

        if self.covariance.ndim == 1:
            self.covariance = np.diag(self.covariance)

        return np.dot(diff, np.dot(np.linalg.inv(self.covariance), diff)) < scale
