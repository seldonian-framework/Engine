from operator import itemgetter
from functools import reduce, partial
import pandas as pd
import autograd.numpy as np

from . import zhat_funcs
from seldonian.utils.stats_utils import *

"""

.. data:: measure_functions_dict
    :type: dict[regime][sub_regime]

    Contains strings that, if appearing in 
    a constraint string, will be recognized
    by the engine as statistical functions with 
    special meaning. Organized by regime and sub-regime. 
    For reference the meaning of each measure function is listed here:
        
        Supervised classification: 

        - 'PR': Positive rate
        - 'NR': Negative rate
        - 'FPR': False positive rate
        - 'TPR': True positive rate
        - 'FNR': False negative rate
        - 'TNR': True negative rate
        - 'ACC': Accuracy
        - 'CM': Confusion matrix (only relevant for multi-class classification)
        
        Supervised regression:
            
        - 'Mean_Error': Mean error
        - 'Mean_Squared_Error': Mean squared error
        
        Reinforcement learning:

        - 'J_pi_new': The performance (expected return of weighted rewards) of the new policy

.. data:: custom_base_node_dict
    :type: dict

    A dictionary mapping the name of a custom 
    base node as it would appear in the 
    constraint string to the class representing it 
    in :py:mod:`.nodes`

"""


class Node(object):
    def __init__(self, name, lower, upper):
        """The base class for all parse tree nodes

        :param name:
            The name of the node
        :type name: str

        :param lower:
            Lower confidence bound
        :type lower: float

        :param upper:
            Upper confidence bound
        :type upper: float

        :ivar index:
            The index of the node in the tree
        :vartype index: int

        :ivar left:
            Left child node
        :vartype left: Node object, defaults to None

        :ivar right:
            Right child node
        :vartype right: Node object, defaults to None

        :ivar will_lower_bound:
            Whether this node needs a lower bound
        :vartype will_lower_bound: bool

        :ivar will_upper_bound:
            Whether this node needs an upper bound
        :vartype will_upper_bound: bool

        """
        self.name = name
        self.index = None
        self.left = None
        self.right = None
        self.lower = lower
        self.upper = upper
        self.will_lower_bound = True
        self.will_upper_bound = True

    def __repr__(self):
        """The string representation of the node.
        Also, what is displayed inside the node box
        in the visual graph
        """
        lower_bracket = "(" if np.isinf(self.lower) else "["
        upper_bracket = ")" if np.isinf(self.upper) else "]"

        lower_str = f"{self.lower:g}" if self.will_lower_bound else "_"
        upper_str = f"{self.upper:g}" if self.will_upper_bound else "_"

        bounds_str = (
            f"{lower_bracket}{lower_str}, {upper_str}{upper_bracket}"
            if (self.lower != None or self.upper != None)
            else "()"
        )

        return "\n".join(
            ["[" + str(self.index) + "]", str(self.name), "\u03B5" + " " + bounds_str]
        )


class BaseNode(Node):
    def __init__(
        self,
        name,
        lower=float("-inf"),
        upper=float("inf"),
        conditional_columns=[],
        **kwargs,
    ):
        """Class for base variable leaf nodes
        in the parse tree.

        :param name:
            The name of the node
        :type name: str
        :param lower:
            Lower confidence bound
        :type lower: float
        :param upper:
            Upper confidence bound
        :type upper: float
        :param conditional_columns:
            When calculating confidence bounds on a measure
            function, condition on these columns being == 1
        :type conditional_columns: List(str)
        :ivar node_type:
            equal to 'base_node'
        :vartype node_type: str
        :ivar delta:
            The share of the confidence put into this node
        :vartype delta: float
        :ivar measure_function_name: str
            The name of the statistical measurement
            function that this node represents, e.g. "FPR".
            Must be contained in measure_functions
            list in :py:mod:`.operators`
        :vartype measure_function_name: str
        """
        super().__init__(name, lower, upper, **kwargs)
        self.conditional_columns = conditional_columns
        self.node_type = "base_node"
        self.delta_lower = None
        self.delta_upper = None
        self.measure_function_name = ""

    def __repr__(self):
        """Overrides Node.__repr__()"""
        return (
            super().__repr__()
            + ", "
            + "\u03B4"
            + f"=({self.delta_lower},{self.delta_upper})"
        )

    def calculate_value(self, **kwargs):
        """
        Calculate the value of the node
        given model weights, etc. This is
        the expected value of the base variable,
        not the bound.
        """
        value = zhat_funcs.evaluate_statistic(
            statistic_name=self.measure_function_name, **kwargs
        )
        return value

    def mask_data(self, dataset, conditional_columns):
        """Mask features and labels using
        a joint AND mask where each of the
        conditional columns is True.

        :param dataset:
            The candidate or safety dataset
        :type dataset: dataset.Dataset object
        :param conditional_columns:
            List of columns for which to create
            the joint AND mask on the dataset
        :type conditional_columns: List(str)

        :return: The masked dataframe
        :rtype: numpy ndarray
        """
        # Figure out indices of sensitive attributes from their column names
        sensitive_col_indices = [
            dataset.sensitive_col_names.index(col) for col in conditional_columns
        ]

        joint_mask = reduce(
            np.logical_and,
            (
                dataset.sensitive_attrs[:, col_index] == 1
                for col_index in sensitive_col_indices
            ),
        )
        if dataset.regime == "supervised_learning":
            if type(dataset.features) == list:
                masked_features = [x[joint_mask] for x in dataset.features]
                masked_labels = [x[joint_mask] for x in dataset.labels]
                # If possible, convert to numpy array. Not always possible,
                # e.g., if features are of different dimensions.
                try:
                    masked_features = np.array(masked_features)
                    masked_labels = np.array(masked_labels)
                except Exception as e:
                    # masked_features and masked_labels stay as lists
                    pass
            else:
                # numpy array
                masked_features = dataset.features[joint_mask]
                masked_labels = dataset.labels[joint_mask]

            return masked_features, masked_labels

        elif dataset.regime == "reinforcement_learning":
            masked_episodes = np.asarray(dataset.episodes)[joint_mask]
            return masked_episodes

    def calculate_data_forbound(self, **kwargs):
        """
        Prepare data inputs
        for confidence bound calculation.
        """
        theta, dataset, model, regime, branch = itemgetter(
            "theta", "dataset", "model", "regime", "branch"
        )(kwargs)

        if branch == "candidate_selection":
            # Then we're in candidate selection
            n_safety = kwargs["n_safety"]

        # If in candidate selection want to use safety data size
        # in bound calculation

        if regime == "supervised_learning":
            # mask the data using the conditional columns, if present

            features = dataset.features
            labels = dataset.labels

            if self.conditional_columns:
                masked_features, masked_labels = self.mask_data(
                    dataset, self.conditional_columns
                )
            else:
                (masked_features, masked_labels) = (features, labels)

            data_dict = {"features": masked_features, "labels": masked_labels}

        elif regime == "reinforcement_learning":
            gamma = model.env_kwargs["gamma"]
            episodes = dataset.episodes

            if self.conditional_columns:
                masked_episodes = self.mask_data(dataset, self.conditional_columns)
            else:
                masked_episodes = episodes

            # Precalculate expected return from behavioral policy
            # using the reward specified by the alt_reward_number
            # These are only ever used in the zhat functions, so
            # they pertain to the constraint, not the primary objective
            if "alt_reward_number" in kwargs:
                # use the alternate reward specified in the constraint string
                # when calculating the return
                alt_reward_number = kwargs["alt_reward_number"]
                alt_reward_index = alt_reward_number - 1
                masked_returns = [
                    weighted_sum_gamma(ep.alt_rewards[:, alt_reward_index], gamma)
                    for ep in masked_episodes
                ]
            else:
                masked_returns = [
                    weighted_sum_gamma(ep.rewards, gamma) for ep in masked_episodes
                ]

            data_dict = {
                "episodes": masked_episodes,
                "weighted_returns": masked_returns,
            }

        return data_dict

    def calculate_bounds(self, **kwargs):
        """Calculate confidence bounds given a bound_method,
        such as t-test.
        """
        if "bound_method" in kwargs:
            bound_method = kwargs["bound_method"]
            if bound_method == "manual":
                # Bounds set by user
                return {"lower": self.lower, "upper": self.upper}

            elif bound_method == "random":
                # Randomly assign lower and upper bounds
                lower, upper = (np.random.randint(0, 2), np.random.randint(2, 4))
                return {"lower": lower, "upper": upper}

            else:
                # Real confidence bound

                # --TODO-- abstract away to support things like
                # getting confidence intervals from bootstrap
                # and RL cases
                estimator_samples = self.zhat(**kwargs)

                if len(estimator_samples) < 5:
                    bounds_dict = {}
                    if self.will_lower_bound:
                        bounds_dict["lower"] = -np.inf
                    if self.will_upper_bound:
                        bounds_dict["upper"] = np.inf
                    return bounds_dict

                branch = kwargs["branch"]
                if branch == "safety_test":
                    datasize = len(estimator_samples)
                elif branch == "candidate_selection":
                    candidate_dataset = kwargs["dataset"]
                    n_candidate = candidate_dataset.num_datapoints
                    n_safety = kwargs["n_safety"]
                    # Want to predict the size of the safety dataset.
                    # We do this using the fraction of candidate data we
                    # get from the estimator
                    datasize = int(
                        round((len(estimator_samples) / n_candidate) * n_safety)
                    )

                data_dict = kwargs["data_dict"]
                bound_kwargs = kwargs
                bound_kwargs["data"] = estimator_samples
                bound_kwargs["datasize"] = datasize

                # If lower and upper are both needed,
                # can't necessarily call lower and upper
                # bound functions separately. Sometimes the joint bound
                # is different from the individual bounds combined
                if self.will_lower_bound and self.will_upper_bound:
                    bound_kwargs["delta_lower"] = self.delta_lower
                    bound_kwargs["delta_upper"] = self.delta_upper
                    if branch == "candidate_selection":
                        lower, upper = self.predict_HC_upper_and_lowerbound(
                            **bound_kwargs
                        )
                    elif branch == "safety_test":
                        lower, upper = self.compute_HC_upper_and_lowerbound(
                            **bound_kwargs
                        )
                    return {"lower": lower, "upper": upper}

                elif self.will_lower_bound:
                    bound_kwargs["delta"] = self.delta_lower
                    if branch == "candidate_selection":
                        lower = self.predict_HC_lowerbound(**bound_kwargs)
                    elif branch == "safety_test":
                        lower = self.compute_HC_lowerbound(**bound_kwargs)
                    return {"lower": lower}

                elif self.will_upper_bound:
                    bound_kwargs["delta"] = self.delta_upper
                    if branch == "candidate_selection":
                        upper = self.predict_HC_upperbound(**bound_kwargs)
                    elif branch == "safety_test":
                        upper = self.compute_HC_upperbound(**bound_kwargs)
                    return {"upper": upper}

                raise AssertionError(
                    "will_lower_bound and will_upper_bound " "cannot both be False"
                )

        else:
            raise RuntimeError("bound_method not specified!")

    def zhat(self, model, theta, data_dict, **kwargs):
        """
        Calculate an unbiased estimate of the
        base variable node.

        :param model: The machine learning model
        :type model: models.SeldonianModel object
        :param theta:
            model weights
        :type theta: numpy ndarray
        :param data_dict:
            Contains inputs to model,
            such as features and labels
        :type data_dict: dict
        """

        return zhat_funcs.sample_from_statistic(
            model=model,
            statistic_name=self.measure_function_name,
            theta=theta,
            data_dict=data_dict,
            **kwargs,
        )

    def predict_HC_lowerbound(self, data, datasize, delta, **kwargs):
        """
        Calculate high confidence lower bound
        that we expect to pass the safety test.
        Used in candidate selection

        :param data:
            Vector containing base variable
            evaluated at each observation in dataset
        :type data: numpy ndarray
        :param datasize:
            The number of observations in the safety dataset
        :type datasize: int
        :param delta:
            Confidence level, e.g. 0.05
        :type delta: float
        """
        if "bound_method" in kwargs:
            bound_method = kwargs["bound_method"]

            if bound_method == "ttest":
                lower = data.mean() - 2 * stddev(data) / np.sqrt(datasize) * tinv(
                    1.0 - delta, datasize - 1
                )
            else:
                raise NotImplementedError(
                    f"Bounding method {bound_method} is not supported"
                )

        return lower

    def predict_HC_upperbound(self, data, datasize, delta, **kwargs):
        """
        Calculate high confidence upper bound
        that we expect to pass the safety test.
        Used in candidate selection

        :param data:
            Vector containing base variable
            evaluated at each observation in dataset
        :type data: numpy ndarray
        :param datasize:
            The number of observations in the safety dataset
        :type datasize: int
        :param delta:
            Confidence level, e.g. 0.05
        :type delta: float
        """
        if "bound_method" in kwargs:
            bound_method = kwargs["bound_method"]
            if bound_method == "ttest":
                lower = data.mean() + 2 * stddev(data) / np.sqrt(datasize) * tinv(
                    1.0 - delta, datasize - 1
                )
            else:
                raise NotImplementedError(
                    f"Bounding method {bound_method} is not supported"
                )

        return lower

    def predict_HC_upper_and_lowerbound(
        self, data, datasize, delta_lower, delta_upper, **kwargs
    ):
        """
        Calculate high confidence lower and upper bounds
        that we expect to pass the safety test.
        Used in candidate selection.

        Depending on the bound_method,
        this is not always equivalent
        to calling predict_HC_lowerbound() and
        predict_HC_upperbound() independently.

        :param data:
            Vector containing base variable
            evaluated at each observation in dataset
        :type data: numpy ndarray
        :param datasize:
            The number of observations in the safety dataset
        :type datasize: int
        :param delta:
            Confidence level, e.g. 0.05
        :type delta: float
        """
        if "bound_method" in kwargs:
            bound_method = kwargs["bound_method"]
            if bound_method == "ttest":
                lower = self.predict_HC_lowerbound(
                    data=data, datasize=datasize, delta=delta_lower, **kwargs
                )
                upper = self.predict_HC_upperbound(
                    data=data, datasize=datasize, delta=delta_upper, **kwargs
                )

            elif bound_method == "manual":
                pass
            else:
                raise NotImplementedError(
                    f"Bounding method {bound_method}" " is not supported"
                )

        return lower, upper

    def compute_HC_lowerbound(self, data, datasize, delta, **kwargs):
        """
        Calculate high confidence lower bound
        Used in safety test

        :param data:
            Vector containing base variable
            evaluated at each observation in dataset
        :type data: numpy ndarray
        :param datasize:
            The number of observations in the safety dataset
        :type datasize: int
        :param delta:
            Confidence level, e.g. 0.05
        :type delta: float
        """
        if "bound_method" in kwargs:
            bound_method = kwargs["bound_method"]
            if bound_method == "ttest":
                lower = data.mean() - stddev(data) / np.sqrt(datasize) * tinv(
                    1.0 - delta, datasize - 1
                )
            else:
                raise NotImplementedError(
                    f"Bounding method {bound_method}" " is not supported"
                )
        return lower

    def compute_HC_upperbound(self, data, datasize, delta, **kwargs):
        """
        Calculate high confidence upper bound
        Used in safety test

        :param data:
            Vector containing base variable
            evaluated at each observation in dataset
        :type data: numpy ndarray
        :param datasize:
            The number of observations in the safety dataset
        :type datasize: int
        :param delta:
            Confidence level, e.g. 0.05
        :type delta: float
        """
        if "bound_method" in kwargs:
            bound_method = kwargs["bound_method"]
            if bound_method == "ttest":
                upper = data.mean() + stddev(data) / np.sqrt(datasize) * tinv(
                    1.0 - delta, datasize - 1
                )
            else:
                raise NotImplementedError(
                    f"Bounding method {bound_method}" " is not supported"
                )

        return upper

    def compute_HC_upper_and_lowerbound(
        self, data, datasize, delta_lower, delta_upper, **kwargs
    ):
        """
        Calculate high confidence lower and upper bounds
        Used in safety test.

        Depending on the bound_method,
        this is not always equivalent
        to calling compute_HC_lowerbound() and
        compute_HC_upperbound() independently.

        :param data:
            Vector containing base variable
            evaluated at each observation in dataset
        :type data: numpy ndarray
        :param datasize:
            The number of observations in the safety dataset
        :type datasize: int
        :param delta:
            Confidence level, e.g. 0.05
        :type delta: float
        """
        if "bound_method" in kwargs:
            bound_method = kwargs["bound_method"]
            if bound_method == "ttest":
                lower = self.compute_HC_lowerbound(
                    data=data, datasize=datasize, delta=delta_lower, **kwargs
                )
                upper = self.compute_HC_upperbound(
                    data=data, datasize=datasize, delta=delta_upper, **kwargs
                )

            elif bound_method == "manual":
                pass
            else:
                raise NotImplementedError(
                    f"Bounding method {bound_method}" " is not supported"
                )
        else:
            raise NotImplementedError(
                "Have not implemented"
                "confidence bounds without the keyword bound_method"
            )

        return lower, upper


class ConfusionMatrixBaseNode(BaseNode):
    def __init__(
        self,
        name,
        cm_true_index,
        cm_pred_index,
        lower=float("-inf"),
        upper=float("inf"),
        conditional_columns=[],
        **kwargs,
    ):
        """A confusion matrix base node.
        Inherits all of the attributes/methods
        of basenode and sets the i,j indices
        of the K x K confusion matrix, C:

        ::

                            Predicted labels
                        | j=0  | j=1  | ... | j=K
                        ---------------------------
                    i=0 | C_00 | C_01 | ... | C_0K|
                        |______|______|_____|_____|
            True    i=1 | C_10 | C_11 | ... | C_1K|
            labels      |______|______|_____|_____|
                    ... | ...  | ...  | ... | ... |
                        |______|______|_____|_____|
                    i=K | C_K0 | C_K1 | ... | C_KK|
                        |______|______|_____|_____|

        :param name:
            The name of the node
        :type name: str
        :param cm_true_index:
            The index of the row in the confusion matrix.
            Rows are the true values
        :param cm_pred_index:
            The index of the column in the confusion matrix.
            Columns are the predicted values
        :param lower:
            Lower confidence bound
        :type lower: float
        :param upper:
            Upper confidence bound
        :type upper: float
        :param conditional_columns:
            When calculating confidence bounds on a measure
            function, condition on these columns being == 1
        :type conditional_columns: List(str)
        """
        super().__init__(
            name=name,
            lower=lower,
            upper=upper,
            conditional_columns=conditional_columns,
            **kwargs,
        )
        self.cm_true_index = cm_true_index
        self.cm_pred_index = cm_pred_index


class MultiClassBaseNode(BaseNode):
    def __init__(
        self,
        name,
        class_index,
        lower=float("-inf"),
        upper=float("inf"),
        conditional_columns=[],
        **kwargs,
    ):
        """A base node for computing
        the classification statistic
        for a single class against all
        other classes. For example,
        if one has 3 classes and wants the
        false positive rate of the first class,
        they would write "FPR_[0]" in their constraint
        and we would calculate the rate at which
        the model predicted class 0 when the true
        label was not class 0 (i.e., class 1 or 2).
        Inherits all of the attributes/methods
        of basenode

        :param name:
            The name of the node, e.g. "FPR_[0]"
        :type name: str
        :param class_index:
            The class index against which to calculate
            the statistic, e.g. false positive rate
        :param lower:
            Lower confidence bound
        :type lower: float
        :param upper:
            Upper confidence bound
        :type upper: float
        :param conditional_columns:
            When calculating confidence bounds on a measure
            function, condition on these columns being == 1
        :type conditional_columns: List(str)
        """
        super().__init__(
            name=name,
            lower=lower,
            upper=upper,
            conditional_columns=conditional_columns,
            **kwargs,
        )
        self.class_index = class_index


class NewPolicyPerformanceBaseNode(BaseNode):
    def __init__(
        self,
        name,
        lower=float("-inf"),
        upper=float("inf"),
        conditional_columns=[],
        **kwargs,
    ):
        """A base node for computing
        the expected return (performance) of a "new" policy parameterization,
        i.e., one proposed during candidate selection.
        Overrides the calculate_value() method to include an
        "on_policy" boolean flag that (if True)
        calculate the expected return via episodes
        generated by the new policy parameterization.
        The default is to use the off-policy estimate
        referenced by self.measure_function_name from the
        behavior policy episodes.

        :param name:
            The name of the node, e.g. "J_pi_new_IS_[1]"
        :type name: str
        :param lower:
            Lower confidence bound
        :type lower: float
        :param upper:
            Upper confidence bound
        :type upper: float
        :param conditional_columns:
            When calculating confidence bounds on a measure
            function, condition on these columns being == 1
        :type conditional_columns: List(str)
        """
        super().__init__(
            name=name,
            lower=lower,
            upper=upper,
            conditional_columns=conditional_columns,
            **kwargs,
        )

    def calculate_value(self, on_policy=False, **kwargs):
        """
        Calculate the value of the node
        given model weights, etc. This is
        the expected value of the base variable,
        not the bound.

        :param on_policy: If True, uses episodes generated by the new policy parameterization
            to calculate the return. If False, estimates the return using an off-policy estimate.
        :type on_policy: Boolean
        """
        if on_policy:
            episodes_new = kwargs["dataset"].episodes
            model = kwargs["model"]
            gamma = model.env_kwargs["gamma"]
            returns_new = np.array(
                [weighted_sum_gamma(ep.rewards, gamma) for ep in episodes_new]
            )
            value = np.mean(returns_new)
        else:
            value = zhat_funcs.evaluate_statistic(
                statistic_name=self.measure_function_name, **kwargs
            )
        return value


class RLAltRewardBaseNode(NewPolicyPerformanceBaseNode):
    def __init__(
        self,
        name,
        alt_reward_number,
        lower=float("-inf"),
        upper=float("inf"),
        conditional_columns=[],
        **kwargs,
    ):
        """A base node for computing
        the IS estimate using an alternate
        reward (i.e. one besides the primary reward).
        There can be an arbitrary number of
        alternate rewards, so the "alt_reward_number"
        attribute allows one to reference the specific
        alternate reward. These are 1-indexed,
        so if one wants to reference the second
        alternate reward, the base node string would be:
        "J_pi_new_IS_[2]"
        Inherits all of the attributes/methods
        of basenode

        :param name:
            The name of the node, e.g. "J_pi_new_IS_[1]"
        :type name: str
        :param alt_reward_number:
            Which alternate reward to use when
            calculating the IS estimate. 1-indexed.
        :param lower:
            Lower confidence bound
        :type lower: float
        :param upper:
            Upper confidence bound
        :type upper: float
        :param conditional_columns:
            When calculating confidence bounds on a measure
            function, condition on these columns being == 1
        :type conditional_columns: List(str)
        """
        super().__init__(
            name=name,
            lower=lower,
            upper=upper,
            conditional_columns=conditional_columns,
            **kwargs,
        )
        self.alt_reward_number = alt_reward_number

    def calculate_value(self, on_policy=False, **kwargs):
        """
        Calculate the value of the node
        given model weights, etc. This is
        the expected value of the base variable,
        not the bound.

        :param on_policy: If True, uses episodes generated by the new policy parameterization
            to calculate the return. If False, estimates the return using an off-policy estimate.
        :type on_policy: Boolean
        """

        if on_policy:
            episodes_new = kwargs["episodes_for_eval"]
            model = kwargs["model"]
            gamma = model.env_kwargs["gamma"]
            alt_reward_index = self.alt_reward_number - 1
            returns_new = [
                weighted_sum_gamma(ep.alt_rewards[:, alt_reward_index], gamma)
                for ep in episodes_new
            ]
            value = np.mean(returns_new)
        else:
            value = zhat_funcs.evaluate_statistic(
                statistic_name=self.measure_function_name, **kwargs
            )
        return value


class MEDCustomBaseNode(BaseNode):
    def __init__(self, name, lower=float("-inf"), upper=float("inf"), **kwargs):
        """
        Custom base node that calculates pair-wise
        mean error differences between male and female
        points. This was used in the Seldonian regression algorithm
        presented by Thomas et al. (2019):
        https://www.science.org/stoken/author-tokens/ST-119/full
        see Figure 2.

        Overrides several parent class methods

        :param name:
            The name of the node
        :type name: str

        :param lower:
            Lower confidence bound
        :type lower: float

        :param upper:
            Upper confidence bound
        :type upper: float

        :ivar delta:
            The share of the confidence put into this node
        :vartype delta: float
        """
        super().__init__(name, lower, upper, **kwargs)

    def calculate_data_forbound(self, **kwargs):
        """
        Overrides same method from parent class, :py:class:`.BaseNode`
        """
        dataset = kwargs["dataset"]
        features = dataset.features
        labels = np.expand_dims(dataset.labels, axis=1)
        sensitive_attrs = dataset.sensitive_attrs

        data_dict = self.precalculate_data(features, labels, sensitive_attrs)

        return data_dict

    def precalculate_data(self, X, Y, S):
        """
        Preconfigure dataset for candidate selection or
        safety test so that it does not need to be
        recalculated on each iteration through the parse tree

        :param X: features
        :type X: pandas dataframe

        :param Y: labels
        :type Y: pandas dataframe
        """
        male_mask = S[:, 0] == 1

        X_male = X[male_mask]
        Y_male = Y[male_mask]
        X_female = X[~male_mask]
        Y_female = Y[~male_mask]
        N_male = len(X_male)
        N_female = len(X_female)
        N_least = min(N_male, N_female)

        # sample N_least from both without repeats
        XY_male = np.hstack([X_male, Y_male])  # column stack
        ix_sample_male = np.random.choice(
            range(len(XY_male)), size=N_least, replace=True
        )
        XY_male = XY_male[ix_sample_male, :]
        X_male = XY_male[:, :-1]
        Y_male = XY_male[:, -1]

        XY_female = np.hstack([X_female, Y_female])  # column stack
        ix_sample_female = np.random.choice(
            range(len(XY_female)), size=N_least, replace=True
        )
        XY_female = XY_female[ix_sample_female, :]
        X_female = XY_female[:, :-1]
        Y_female = XY_female[:, -1]

        data_dict = {
            "X_male": X_male,
            "Y_male": Y_male,
            "X_female": X_female,
            "Y_female": Y_female,
        }
        datasize = N_least
        return data_dict

    def zhat(self, model, theta, data_dict, **kwargs):
        """
        Pair up male and female columns and compute a vector of:
        (y_i - y_hat_i | M) - (y_j - y_hat_j | F).
        There may not be the same number of male and female rows
        so the number of pairs is min(N_male,N_female)

        :param model: machine learning model
        :type model: models.SeldonianModel object

        :param theta:
            model weights
        :type theta: numpy ndarray

        :param data_dict:
            contains inputs to model,
            such as features and labels
        :type data_dict: dict
        """
        X_male = data_dict["X_male"]
        Y_male = data_dict["Y_male"]
        X_female = data_dict["X_female"]
        Y_female = data_dict["Y_female"]

        prediction_male = model.predict(theta, X_male)
        mean_error_male = prediction_male - Y_male

        prediction_female = model.predict(theta, X_female)
        mean_error_female = prediction_female - Y_female

        return mean_error_male - mean_error_female


class CVaRSQeBaseNode(BaseNode):
    def __init__(self, name, lower=float("-inf"), upper=float("inf"), **kwargs):
        """
        Custom base node that calculates the upper and
        lower bounds on CVaR_alpha (with alpha fixed to 0.1)
        of the squared error. We are using the positive
        definition of CVaR_alpha, i.e. "...the expected value
        if we only consider the samples that are at least VaR_alpha,"
        where VaR_alpha "... is the largest value such that at least 100*alpha%
        of samples will be larger than it." - Thomas & Miller 2019:
        https://people.cs.umass.edu/~pthomas/papers/Thomas2019.pdf
        See Theorem 3 for upper bound and Theorem 4 for lower bound

        Overrides several parent class methods

        :param name:
            The name of the node
        :type name: str
        :param lower:
            Lower confidence bound
        :type lower: float
        :param upper:
            Upper confidence bound
        :type upper: float
        :ivar delta:
            The share of the confidence put into this node
        :vartype delta: float
        :ivar alpha:
            The probability threshold used to define CVAR
        :vartype alpha: float
        """
        super().__init__(name, lower, upper, **kwargs)
        self.alpha = 0.1

    def calculate_value(self, **kwargs):
        """
        Calculate the actual value of CVAR_alpha,
        not the bound.
        """
        from seldonian.models import objectives

        model = kwargs["model"]
        theta = kwargs["theta"]
        data_dict = kwargs["data_dict"]

        # Get squashed squared errors
        X = data_dict["features"]
        y = data_dict["labels"]
        squared_errors = zhat_funcs.vector_Squared_Error(model, theta, X, y)
        # sort
        Z = np.array(sorted(squared_errors))
        # Now calculate cvar
        percentile_thresh = (1 - self.alpha) * 100
        # calculate var_alpha
        var_alpha = np.percentile(Z, percentile_thresh)
        # cvar is the mean of all values >= var_alpha
        cvar_mask = Z >= var_alpha
        Z_cvar = Z[cvar_mask]
        cvar = np.mean(Z_cvar)
        return cvar

    def calculate_bounds(self, **kwargs):
        from seldonian.models import objectives

        """Calculate confidence bounds using the concentration 
        inequalities in Thomas & Miller 2019, Theorem's 3 and 4.
        """
        branch = kwargs["branch"]
        model = kwargs["model"]
        theta = kwargs["theta"]
        data_dict = kwargs["data_dict"]

        X = data_dict["features"]
        y = data_dict["labels"]

        # import matplotlib.pyplot as plt
        # y_hat = model.predict(theta,X)
        # def MSE(y_true,y_pred):
        #     N = len(y_true)
        #     return 1/N*sum(pow(y_true-y_pred,2))
        # mse = MSE(y,y_hat)
        # fig = plt.figure()
        # ax = fig.add_subplot(1,1,1)
        # ax.scatter(X,y,s=5,label='y clipped')
        # print(f"MSE = {mse:.5f}")
        # ax.scatter(X,y_hat,color='r',s=5,alpha=0.1,label='squashed using 1.5x bound inflation')
        # ax.set_xlabel("X")
        # ax.set_ylabel("Y")
        # ax.set_title("X=N(0,1), Y=X+N(0,0.2)")
        # ax.legend()
        # plt.show()
        # input("Wait!")
        # assume labels have been clipped to -3,3
        # theoretical min and max (not actual min and max) are:
        y_min, y_max = -3, 3
        # Increase bounds of y_hat to s times the size of y bounds
        s = 2.0
        y_hat_min = y_min * (1 + s) / 2 + y_max * (1 - s) / 2
        y_hat_max = y_max * (1 + s) / 2 + y_min * (1 - s) / 2

        min_squared_error = 0
        max_squared_error = max(pow(y_hat_max - y_min, 2), pow(y_max - y_hat_min, 2))

        squared_errors = zhat_funcs.vector_Squared_Error(model, theta, X, y)

        a = min_squared_error
        b = max_squared_error
        # Need to sort squared errors to get Z1, ..., Zn
        sorted_squared_errors = sorted(squared_errors)

        if branch == "safety_test":
            datasize = len(sorted_squared_errors)
        elif branch == "candidate_selection":
            candidate_dataset = kwargs["dataset"]
            n_candidate = candidate_dataset.num_datapoints
            n_safety = kwargs["n_safety"]
            # Want to predict the size of the safety dataset.
            # We do this using the fraction of candidate data we
            # get from the estimator
            datasize = int(round((len(sorted_squared_errors) / n_candidate) * n_safety))

        bound_kwargs = {
            "Z": sorted_squared_errors,
            "datasize": datasize,
            "a": a,
            "b": b,
        }

        if self.will_lower_bound and self.will_upper_bound:
            bound_kwargs["delta_lower"] = self.delta_lower
            bound_kwargs["delta_upper"] = self.delta_upper
            if branch == "candidate_selection":
                lower = self.predict_HC_lowerbound(**bound_kwargs)
                upper = self.predict_HC_upperbound(**bound_kwargs)
            elif branch == "safety_test":
                lower = self.compute_HC_lowerbound(**bound_kwargs)
                upper = self.compute_HC_upperbound(**bound_kwargs)
            return {"lower": lower, "upper": upper}

        elif self.will_lower_bound:
            bound_kwargs["delta"] = self.delta_lower
            if branch == "candidate_selection":
                lower = self.predict_HC_lowerbound(**bound_kwargs)
            elif branch == "safety_test":
                lower = self.compute_HC_lowerbound(**bound_kwargs)
            return {"lower": lower}

        elif self.will_upper_bound:
            bound_kwargs["delta"] = self.delta_upper
            if branch == "candidate_selection":
                upper = self.predict_HC_upperbound(**bound_kwargs)
            elif branch == "safety_test":
                upper = self.compute_HC_upperbound(**bound_kwargs)
            return {"upper": upper}

        raise AssertionError(
            "will_lower_bound and will_upper_bound cannot both be False"
        )

    def predict_HC_lowerbound(self, Z, delta, datasize, a, **kwargs):
        """
        Calculate high confidence lower bound
        that we expect to pass the safety test.
        Used in candidate selection

        :param Z:
            Vector containing sorted squared errors
        :type Z: numpy ndarray of length n_candidate
        :param delta:
            Confidence level, e.g. 0.05
        :type delta: float
        :param datasize:
            The (predicted) number of observations in the safety dataset
        :type datasize: int
        :param a: The minimum possible value of the squared error
        :type a: float
        """
        Znew = Z.copy()
        Znew = np.array([a] + Znew)
        n_candidate = len(Znew) - 1

        sqrt_term = np.sqrt((np.log(1 / delta)) / (2 * datasize))
        max_term = np.maximum(
            np.zeros(n_candidate),
            np.minimum(
                np.ones(n_candidate),
                np.arange(n_candidate) / n_candidate + 2 * sqrt_term,
            )
            - (1 - self.alpha),
        )

        lower = Znew[-1] - 1 / self.alpha * sum(np.diff(Znew) * max_term)

        return lower

    def predict_HC_upperbound(self, Z, delta, datasize, b, **kwargs):
        """
        Calculate high confidence upper bound
        that we expect to pass the safety test.
        Used in candidate selection

        :param Z:
            Vector containing sorted squared errors
        :type Z: numpy ndarray of length n_candidate
        :param delta:
            Confidence level, e.g. 0.05
        :type delta: float
        :param datasize:
            The (predicted) number of observations in the safety dataset
        :type datasize: int
        :param b: The maximum possible value of the squared error
        :type b: float
        """
        assert 0 < delta <= 0.5
        Znew = Z.copy()
        Znew.append(b)

        n_candidate = len(Znew) - 1

        # sqrt term is independent of loop index
        sqrt_term = np.sqrt((np.log(1 / delta)) / (2 * datasize))
        max_term = np.maximum(
            np.zeros(n_candidate),
            (1 + np.arange(n_candidate)) / n_candidate
            - 2 * sqrt_term
            - (1 - self.alpha),
        )
        upper = Znew[-1] - (1 / self.alpha) * sum(np.diff(Znew) * max_term)

        return upper

    def compute_HC_lowerbound(self, Z, delta, datasize, a, **kwargs):
        """
        Calculate high confidence lower bound
        Used in safety test.

        :param Z:
            Vector containing sorted squared errors
        :type Z: numpy ndarray of length datasize
        :param delta:
            Confidence level, e.g. 0.05
        :type delta: float
        :param datasize:
            The number of observations in the safety dataset
        :type datasize: int
        :param a: The minimum possible value of the squared error
        :type a: float
        """
        Znew = Z.copy()
        Znew = np.array([a] + Znew)
        n_candidate = len(Znew) - 1

        sqrt_term = np.sqrt((np.log(1 / delta)) / (2 * datasize))
        max_term = np.maximum(
            np.zeros(datasize),
            np.minimum(np.ones(datasize), np.arange(datasize) / datasize + sqrt_term)
            - (1 - self.alpha),
        )

        lower = Znew[-1] - 1 / self.alpha * sum(np.diff(Znew) * max_term)

        return lower

    def compute_HC_upperbound(self, Z, delta, datasize, b, **kwargs):
        """
        Calculate high confidence upper bound
        Used in safety test

        :param Z:
            Vector containing sorted squared errors
        :type Z: numpy ndarray of length datasize
        :param delta:
            Confidence level, e.g. 0.05
        :type delta: float
        :param datasize:
            The number of observations in the safety dataset
        :type datasize: int
        :param b: The maximum possible value of the squared error
        :type b: float
        """
        assert 0 < delta <= 0.5
        Znew = Z.copy()
        Znew.append(b)
        # sqrt term is independent of loop index
        sqrt_term = np.sqrt((np.log(1 / delta)) / (2 * datasize))
        max_term = np.maximum(
            np.zeros(datasize),
            (1 + np.arange(datasize)) / datasize - sqrt_term - (1 - self.alpha),
        )
        upper = Znew[-1] - 1 / self.alpha * sum(np.diff(Znew) * max_term)

        return upper


class ConstantNode(Node):
    def __init__(self, name, value, **kwargs):
        """
        Class for constant leaf nodes
        in the parse tree. Sets lower and upper
        bound as the value of the constant.

        :param name:
            The name of the node
        :type name: str
        :param value:
            The value of the constant the node represents
        :type value: float
        :ivar node_type:
            'constant_node'
        :vartype node_type: str
        """
        super().__init__(name=name, lower=value, upper=value, **kwargs)
        self.value = value
        self.node_type = "constant_node"


class InternalNode(Node):
    def __init__(self, name, lower=float("-inf"), upper=float("inf"), **kwargs):
        """
        Class for internal (non-leaf) nodes
        in the parse tree.
        These represent operators, such as +,-,*,/ etc.

        :param name:
            The name of the node, which is the
            string representation of the operation
            the node performs
        :type name: str
        :param lower:
            Lower confidence bound
        :type lower: float
        :param upper:
            Upper confidence bound
        :type upper: float
        """
        super().__init__(name, lower, upper, **kwargs)
        self.node_type = "internal_node"


custom_base_node_dict = {
    "MED_MF": MEDCustomBaseNode,
    "CVaRSQE": CVaRSQeBaseNode,
}

measure_functions_dict = {
    "supervised_learning": {
        "classification": [
            "PR",
            "NR",
            "FPR",
            "TPR",
            "FNR",
            "TNR",
            "ACC",
        ],
        "multiclass_classification": [
            "CM",
            "PR",
            "NR",
            "FPR",
            "TPR",
            "FNR",
            "TNR",
            "ACC",
        ],
        "regression": ["Mean_Error", "Mean_Squared_Error"],
    },
    "reinforcement_learning": {
        "all": ["J_pi_new_IS", "J_pi_new_PDIS", "J_pi_new_WIS", "J_pi_new_US"]
    },
}

subscriptable_measure_functions = [
    "CM",
    "PR",
    "NR",
    "FPR",
    "TNR",
    "TPR",
    "FNR",
    "J_pi_new_IS",
    "J_pi_new_PDIS",
    "J_pi_new_US",
    "J_pi_new_WIS",
]
