""" Wrapper module for hyperparameter selection for Seldonian algorithms """

import autograd.numpy as np
import pandas as pd

import os
import copy
import time
import scipy
import pickle
import warnings
import itertools
import multiprocessing as mp
from tqdm import tqdm
from functools import partial
from concurrent.futures import ProcessPoolExecutor

from seldonian.seldonian_algorithm import SeldonianAlgorithm
from seldonian.dataset import SupervisedDataSet, RLDataSet
from seldonian.candidate_selection.candidate_selection import CandidateSelection
from seldonian.safety_test.safety_test import SafetyTest
from seldonian.models import objectives
from seldonian.utils.io_utils import load_pickle
from seldonian.utils.stats_utils import tinv

class HyperSchema(object):
    def __init__(self,hyper_dict):
        """ Container for all hyperparameters one wants to tune
        and their possible values. Example:
        
        :param hyper_dict: Hyperparameter dictionary adhering to the following format:
            keys: names of hyperparameters
            values: dictionary with keys: ["values","hyper_type","default"], where
                "values" is the list of parameter values to run and 
                "hyper_type" is one of ["optimization","model","SA"], specifying 
                the part of the algorithm in which the hyperparameter will be injected,
                and "default" is the default value that will be returned if there is not 
                enough data for the given candidate split to do hyperparameter selection.
            Example: 
            hyper_dict = {
                "alpha_theta": {
                    "values":[0.001,0.005,0.05],
                    "hyper_type":"optimization",
                    "default": 0.001
                },
                "num_iters": {
                    "values":[500,1000],
                    "hyper_type":"optimization",
                    "default":500
                },
                "bound_inflation_factor": {
                    "values":[1.0,2.0,3.0],
                    "hyper_type":"SA",
                    "default": 2.0
                },
            }
        """
        self.allowed_optimization_hyperparams = [
            "alpha_theta",
            "alpha_lamb",
            "beta_velocity",
            "beta_rmsprop",
            "batch_size",
            "n_epochs",
            "num_iters"
        ]
        self.allowed_SA_hyperparams = [
            "bound_inflation_factor",
            "frac_data_in_safety",
            "delta_split_dict"
        ]
        self.hyper_dict = self._validate(hyper_dict)
        
        
    def _validate(self,hyper_dict):
        """ Check that the hyperparameter dictionary is formatted properly
        and contains valid hyperparameters. Model hyperparameters are specific 
        to the model so we can't know what they might be ahead of time. The
        error will be caught elsewhere when the model is instantiated 
        if there is an invalid parameter name.
        """
        for hp in hyper_dict:
            if not isinstance(hyper_dict[hp],dict):
                raise RuntimeError(f"hyper_dict['{hp}'] is not a dictionary. ")
            
            for w in ["values","hyper_type"]:
                if w not in hyper_dict[hp]:
                    raise KeyError(f"hyper_dict['{hp}'] must have the key: '{w}'. ")
            
            if len(hyper_dict[hp]["values"]) < 1:
                raise ValueError(f"hyper_dict['{hp}']['values'] must have at least one value")
            
            if hyper_dict[hp]["hyper_type"] not in ["optimization","model","SA"]:
                raise ValueError(f"hyper_dict['{hp}']['hyper_type'] must be one of ['optimization','model','SA']")
            
            if hyper_dict[hp]["hyper_type"] == "optimization" and hp not in self.allowed_optimization_hyperparams:
                raise ValueError(f"{hp} is not an allowed optimization hyperparameter")
            
            if hyper_dict[hp]["hyper_type"] == "SA" and hp not in self.allowed_SA_hyperparams:
                raise ValueError(f"{hp} is not an allowed hyperparameter of the Seldonian algorithm")
        return hyper_dict   


class HyperparamSearch:
    def __init__(
            self, 
            spec, 
            hyperparam_spec,
            results_dir,
    ):
        """Object for finding the best hyperparameters to use to optimize for probability
        of returning a safe solution for Seldonian algorithms. 
        
        Note: currently only implemented for finding optimal train/safety data split.

        List of hyperparameters to optimize:
        - Percentage of data in candidate and safety datasets

        :param spec: The specification object with the complete
                set of parameters for running the Seldonian algorithm
        :type spec: :py:class:`.Spec` object
        :param hyperparam_spec: The specification object with the complete
                set of parameters for doing hyparpameter selection
        :type spec: :py:class:`.HyperparameterSelectionSpec` object
        """
        self.spec = spec
        self.hyperparam_spec = hyperparam_spec 
        self.results_dir = results_dir

        # Sort frac data in safety 
        self.all_frac_data_in_safety = \
                self.hyperparam_spec.hyper_schema.hyper_dict["frac_data_in_safety"]["values"]
        self.all_frac_data_in_safety.sort(reverse=True) # Start with most data in safety.

        self.parse_trees = self.spec.parse_trees
        # user can pass a dictionary that specifies
        # the bounding method for each base node
        # any base nodes not in this dictionary will
        # be bounded using the default method
        self.base_node_bound_method_dict = self.spec.base_node_bound_method_dict
        if self.base_node_bound_method_dict != {}:
            all_pt_constraint_strs = [pt.constraint_str for pt in self.parse_trees]
            for constraint_str in self.base_node_bound_method_dict:
                this_bound_method_dict = self.base_node_bound_method_dict[
                    constraint_str
                ]
                # figure out which parse tree this comes from
                this_pt_index = all_pt_constraint_strs.index(constraint_str)
                this_pt = self.parse_trees[this_pt_index]
                # change the bound method for each node provided
                for node_name in this_bound_method_dict:
                    this_pt.base_node_dict[node_name][
                        "bound_method"
                    ] = this_bound_method_dict[node_name]

        self.dataset = self.spec.dataset
        self.regime = self.dataset.regime
        self.meta = self.dataset.meta

        self.model = self.spec.model
        if self.regime == "supervised_learning": self.sub_regime = self.spec.sub_regime

        if self.spec.primary_objective is None:
            if self.regime == "reinforcement_learning":
                self.spec.primary_objective = objectives.IS_estimate
            elif self.regime == "supervised_learning":
                if self.spec.sub_regime in ["classification", "binary_classification"]:
                    self.spec.primary_objective = objectives.binary_logistic_loss
                elif self.spec.sub_regime == "multiclass_classification":
                    self.spec.primary_objective = objectives.multiclass_logistic_loss
                elif self.spec.sub_regime == "regression":
                    self.spec.primary_objective = objectives.Mean_Squared_Error


    def get_safety_size(
            self,
            n_total,
            frac_data_in_safety
    ):
        """Determine the number of data points in the safety dataset.

        :param n_total: the size of the total dataset
        :type n_total: int
        :param frac_data_in_safety: fraction of data used in safety test,
                the remaining fraction will be used in candidate selection
        :type frac_data_in_safety: float

        :return: n_safety, the desired size of the safety dataset
        :rtype: int
        """
        n_safety = int(frac_data_in_safety * n_total)

        # If have at least 4 datapoints, make sure that each set gets at least 2.
        if n_total >= 4:
            n_safety = max(n_safety, 2)  # >=2 point in safety
            n_safety = min(n_safety, n_total - 2)  # >=2 point in selection

        return n_safety


    def candidate_safety_combine(
            self,
            candidate_dataset,
            safety_dataset
    ):
        """Combine candidate_dataset and safety_dataset into a full dataset.
        The data will be joined so that the candidate data comes before the 
        safety data.

        :param candidate_dataset: a dataset object containing data
        :type candiddate_dataset: :py:class:`.DataSet` object
        :param safety_dataset: a dataset object containing data
        :type safety_dataset: :py:class:`.DataSet` object

        :return: combinded_dataset, a dataset containing candidate and safety dataset
        :rtype: :py:class:`.DataSet` object
        """
        if self.regime == "supervised_learning":
            combined_num_datapoints = (candidate_dataset.num_datapoints +
                    safety_dataset.num_datapoints)
            # Combine features.
            if ((type(candidate_dataset.features) == list) and 
                    (type(safety_dataset.features) == list)):
                combined_features = [f_c + f_s for (f_c, f_s) in 
                        zip(candidate_dataset.features, safety_dataset.features)]
            else:
                combined_features = np.concatenate((candidate_dataset.features, safety_dataset.features),
                        axis=0)
            # Compute labels - must be a numpy array.
            combined_labels = np.concatenate((candidate_dataset.labels, safety_dataset.labels), axis=0)

            # Combine sensitive attributes. Must be a numpy array.
            combined_sensitive_attrs = np.concatenate((candidate_dataset.sensitive_attrs, 
                safety_dataset.sensitive_attrs), axis=0)

            # Create a dataset.
            combined_dataset = SupervisedDataSet(
                    features=combined_features,
                    labels=combined_labels,
                    sensitive_attrs=combined_sensitive_attrs,
                    num_datapoints=combined_num_datapoints,
                    meta=candidate_dataset.meta)


        elif self.regime == "reinforcement_learning":
            # TODO: Finish implementing this.
            raise NotImplementedError(
                    "Creating bootstrap sampled datasets not yet implemented for "
                    "reinforcement_learning regime")

        return combined_dataset


    def candidate_safety_split(
            self,
            dataset,
            frac_data_in_safety
    ):
        """Split features, labels and sensitive attributes
        into candidate and safety sets according to frac_data_in_safety

        :param dataset: a dataset object containing data
        :type dataset: :py:class:`.DataSet` object
        :param frac_data_in_safety: Fraction of data used in safety test.
                The remaining fraction will be used in candidate selection
        :type frac_data_in_safety: float

        :return: F_c,F_s,L_c,L_s,S_c,S_s
                where F=features, L=labels, S=sensitive attributes
        :rtype: Tuple
        """
        n_points_tot = dataset.num_datapoints
        n_safety = self.get_safety_size(n_points_tot, frac_data_in_safety)
        n_candidate = n_points_tot - n_safety

        if self.regime == "supervised_learning":
            # Split features
            if type(dataset.features) == list:
                F_c = [x[:n_candidate] for x in dataset.features]
                F_s = [x[n_candidate:] for x in dataset.features]
            else:
                F_c = dataset.features[:n_candidate]
                F_s = dataset.features[n_candidate:]
            # Split labels - must be numpy array
            L_c = dataset.labels[:n_candidate]
            L_s = dataset.labels[n_candidate:]

            # Split sensitive attributes - must be numpy array
            S_c = dataset.sensitive_attrs[:n_candidate]
            S_s = dataset.sensitive_attrs[n_candidate:]
            return F_c, F_s, L_c, L_s, S_c, S_s, n_candidate, n_safety

        elif self.regime == "reinforcement_learning":
            # Split episodes
            E_c = dataset.episodes[0:n_candidate]
            E_s = dataset.episodes[n_candidate:]

            # Split sensitive attributes - must be numpy array
            S_c = dataset.sensitive_attrs[:n_candidate]
            S_s = dataset.sensitive_attrs[n_candidate:]
            return E_c, E_s, S_c, S_s, n_candidate, n_safety


    def create_shuffled_dataset(
            self,
            dataset
    ):
        """Create new dataset containing the same data as the given original dataset,
            but with the data shuffled in a new order.

        :param dataset: a dataset object containing data
        :type dataset: :py:class:`.DataSet` object

        :return: shuffled_dataset, a dataset with same points in dataset, but shuffled.
        :rtype: :py:class:`.DataSet` object
        """
        ix_shuffle = np.arange(dataset.num_datapoints)
        np.random.shuffle(ix_shuffle)

        # features can be list of arrays or a single array
        if type(dataset.features) == list:
            resamp_features = [x[ix_shuffle] for x in flist]
        else:
            resamp_features = dataset.features[ix_shuffle] 


        # labels and sensitive attributes must be arrays
        resamp_labels = dataset.labels[ix_shuffle]

        if isinstance(dataset.sensitive_attrs, np.ndarray):
            resamp_sensitive_attrs = dataset.sensitive_attrs[ix_shuffle]
        else:
            resamp_sensitive_attrs = []

        shuffled_dataset = SupervisedDataSet(
            features=resamp_features,
            labels=resamp_labels,
            sensitive_attrs=resamp_sensitive_attrs,
            num_datapoints=dataset.num_datapoints,
            meta=dataset.meta,
        )

        return shuffled_dataset


    def create_dataset(
            self,
            dataset,
            frac_data_in_safety,
            shuffle=False
    ):
        """Partition data to create candidate and safety dataset according to
            frac_data_in_safety. 

        :param dataset: a dataset object containing data
        :type dataset: :py:class:`.DataSet` object
        :param frac_data_in_safety: fraction of data used in safety test,
                the remaining fraction will be used in candidate selection
        :type frac_data_in_safety: float
        :param shuffle: bool indicating if we should shuffle the dataset before 
                splitting it into candidate and safety datasets
        :type shuffle: bool 

        :return: (candidate_dataset, safety_dataset). candidate_dataset
                and safety_datasets are the resulting datasets after partitioning
                the dataset.
        :rtype: Tuple containing two `.DataSet` objects.
        """
        if shuffle:
            dataset = self.create_shuffled_dataset(dataset)

        if self.regime == "supervised_learning":

            if dataset.num_datapoints < 4:
                warning_msg = (
                    "Warning: not enough data to " "run the Seldonian algorithm."
                )
                warnings.warn(warning_msg)

            # Split the data.
            (   candidate_features,
                safety_features,
                candidate_labels,
                safety_labels,
                candidate_sensitive_attrs,
                safety_sensitive_attrs,
                n_candidate,
                n_safety
            ) = self.candidate_safety_split(dataset, frac_data_in_safety)

            candidate_dataset = SupervisedDataSet(
                features=candidate_features,
                labels=candidate_labels,
                sensitive_attrs=candidate_sensitive_attrs,
                num_datapoints=n_candidate,
                meta=dataset.meta,
            )

            safety_dataset = SupervisedDataSet(
                features=safety_features,
                labels=safety_labels,
                sensitive_attrs=safety_sensitive_attrs,
                num_datapoints=n_safety,
                meta=dataset.meta,
            )

            if candidate_dataset.num_datapoints < 2 or safety_dataset.num_datapoints < 2:
                warning_msg = (
                    "Warning: not enough data to " "run the Seldonian algorithm."
                )
                warnings.warn(warning_msg)
            if self.spec.verbose:
                print(f"Safety dataset has {safety_dataset.num_datapoints} datapoints")
                print(f"Candidate dataset has {candidate_dataset.num_datapoints} datapoints")

        elif self.regime == "reinforcement_learning":

            # Split the data.
            (   candidate_episodes,
                safety_episodes,
                candidate_sensitive_attrs,
                safety_sensitive_attrs,
                n_candidate,
                n_safety,
            ) = self.candidate_safety_split(dataset, frac_data_in_safety)

            candidate_dataset = RLDataSet(
                episodes=candidate_episodes,
                sensitive_attrs=candidate_sensitive_attrs,
                meta=self.meta,
            )

            safety_dataset = RLDataSet(
                episodes=safety_episodes,
                sensitive_attrs=safety_sensitive_attrs,
                meta=self.meta,
            )

            print(f"Safety dataset has {safety_dataset.num_datapoints} episodes")
            print(f"Candidate dataset has {candidate_dataset.num_datapoints} episodes")

        return candidate_dataset, safety_dataset


    def bootstrap_sample_dataset(
            self,
            dataset,
            n_bootstrap_samples,
    ):
        """Bootstrap sample a dataset of size n_bootstrap_samples from the data points
            in dataset.

        :param dataset: The original dataset from which to resample
        :type dataset: pandas DataFrame
        :param n_bootstrap_samples: The size of the bootstrapped dataset
        :type n_bootstrap_samples: int
        :param savename: Path to save the bootstrapped dataset.
        :type savename: str
        """ 
        if self.regime == "supervised_learning":
            ix_resamp = np.random.choice(
                range(dataset.num_datapoints), n_bootstrap_samples, replace=True
            )
            # features can be list of arrays or a single array
            if type(dataset.features) == list:
                resamp_features = [x[ix_resamp] for x in flist]
            else:
                resamp_features = dataset.features[ix_resamp]

            # labels and sensitive attributes must be arrays
            resamp_labels = dataset.labels[ix_resamp]
            if isinstance(dataset.sensitive_attrs, np.ndarray):
                resamp_sensitive_attrs = dataset.sensitive_attrs[ix_resamp]
            else:
                resamp_sensitive_attrs = []

            bootstrap_dataset = SupervisedDataSet(
                features=resamp_features,
                labels=resamp_labels,
                sensitive_attrs=resamp_sensitive_attrs,
                num_datapoints=n_bootstrap_samples,
                meta=dataset.meta,
            )

            return bootstrap_dataset

        elif self.regime == "reinforcement_learning":
            # TODO: Finish implementing this.
            raise NotImplementedError(
                    "Creating bootstrap sampled datasets not yet implemented for "
                    "reinforcement_learning regime")


    def generate_all_bootstrap_datasets(
            self,
            candidate_dataset,
            est_frac_data_in_safety,
            n_bootstrap_samples_candidate,
            n_bootstrap_samples_safety,
            bootstrap_savedir,
    ):
        """Utility function for supervised learning to generate the
        resampled datasets to use in each bootstrap trial. Resamples (with replacement)
        features, labels and sensitive attributes to create 
        self.hyperparam_spec.n_bootstrap_trials versions of these 

        :param candidate_dataset: Dataset object containing candidate solution dataset.
                This is the dataset we will be bootstrap sampling from.
        :type candidate_dataset: :py:class:`.DataSet` object
        :param est_frac_data_in_safety: fraction of data in safety set that we want to 
                        estimate the probabiilty of returning a solution for
        :type est_frac_data_in_safety: float
        :param n_bootstrap_samples_candidate: The size of the candidate selection 
                bootstrapped dataset
        :type n_bootstrap_samples_candidate: int
        :param n_bootstrap_samples_safety: The size of the safety bootstrapped dataset
        :type n_bootstrap_safety: int
        :param bootstrap_savedir: The root diretory to save all the bootstrapped datasets.
        :type bootstrap_savedir: str
        """
        created_trials = [] # Stores trial number of the datasets that were created.

        # If not enough datapoints
        if candidate_dataset.num_datapoints < 4:
            return created_trials 

        dataset_save_subdir = os.path.join(bootstrap_savedir, 
                f"future_safety_frac_{est_frac_data_in_safety:.2f}", "bootstrap_datasets")
        bs_result_subdir = os.path.join(bootstrap_savedir,
                f"future_safety_frac_{est_frac_data_in_safety:.2f}", "bootstrap_results")
        os.makedirs(dataset_save_subdir, exist_ok=True) 

        for bootstrap_trial_i in range(self.hyperparam_spec.n_bootstrap_trials):
            # Where to save bootstrapped dataset.
            bootstrap_datasets_savename = os.path.join(dataset_save_subdir, 
                    f"bootstrap_datasets_trial_{bootstrap_trial_i}.pkl")
            bs_result_savename = os.path.join(bs_result_subdir, 
                    f"trial_{bootstrap_trial_i}_result.pkl")

            # Only create datasets if not already run trial, and dataset not already existing.
            if not(os.path.exists(bs_result_savename) or \
                    os.path.exists(bootstrap_datasets_savename)):
                created_trials.append(bootstrap_trial_i)
                bootstrap_datasets_dict = dict() # Will store all the datasets.

                # Bootstrap sample candidate selection and safety datasets.
                if self.hyperparam_spec.use_bs_pools:
                    # Partition candidate_dataset into pools to bootstrap 
                    (bootstrap_pool_candidate, bootstrap_pool_safety) = self.create_dataset(
                            candidate_dataset, est_frac_data_in_safety, shuffle=True)

                    # Sample from pools.
                    bootstrap_datasets_dict["candidate"] = self.bootstrap_sample_dataset(
                            bootstrap_pool_candidate, n_bootstrap_samples_candidate)
                    bootstrap_datasets_dict["safety"] = self.bootstrap_sample_dataset(
                            bootstrap_pool_safety, n_bootstrap_samples_safety)

                else: # Sample directly from candidate datset.
                    bootstrap_datasets_dict["candidate"] = self.bootstrap_sample_dataset(
                            candidate_dataset, n_bootstrap_samples_candidate)
                    bootstrap_datasets_dict["safety"] = self.bootstrap_sample_dataset(
                            candidate_dataset, n_bootstrap_samples_safety)

                # Save datasets.
                with open(bootstrap_datasets_savename, "wb") as outfile:
                    pickle.dump(bootstrap_datasets_dict, outfile)
                    if self.spec.verbose:
                        print(f"Saved {bootstrap_datasets_savename}")

        return created_trials


    def set_spec_with_hyperparam_setting(
            self,
            spec,
            hyperparam_setting
    ):
        """
        Update spec according to hyperparam_setting.

        :type hyperparam_setting: tuple containing hyperparameter values that should be
            set for this bootstrap experiment (if not given will use default from self.spec)
        :type hyperparam_setting: tuple of tuples, where each inner tuple takes the form
            (hyperparameter name, hyperparameter type, hyperparameter value)
                Example:
                (("alpha_theta", "optimization", 0.001), ("num_iters", "optimization", 500))
        """
        if hyperparam_setting is not None:
            for (hyper_name, hyper_type, hyper_value) in hyperparam_setting:
                if hyper_type == "optimization":
                    spec.optimization_hyperparams[hyper_name] = hyper_value
                elif hyper_type == "model":
                    # TODO: Not yet implemented
                    raise NotImplementedError(
                        f"Setting hyperparameter type {hyper_type} in hyperparameter selection"
                        " is not yet implemented"
                    )
                elif hyper_type == "SA":
                    raise NotImplementedError(
                        f"Setting hyperparameter type {hyper_type} in hyperparameter selection"
                        " is not yet implemented"
                    )
                else:
                    raise ValueError(
                        f"{hyper_type} is not a valid hyperparameter type"
                    )

        return spec


    def create_bootstrap_trial_spec(
            self,
            bootstrap_trial_i,
            frac_data_in_safety, 
            bootstrap_savedir,
            hyperparam_setting=None,
    ):
        """Create the spec to run this iteration of the bootstrap trial.

        :param bootstrap_trial_i: Indicates which trial we are currently running
        :type bootstrap_trial_i: int
        :param frac_data_in_safety: fraction of data used in safety test to split the 
            datasets for the trial.
        :type frac_data_in_safety: float
        :param bootstrap_savedir: The root diretory to save all the bootstrapped datasets.
        :type bootstrap_savedir: str
        """
        spec_for_bootstrap_trial = copy.deepcopy(self.spec)

        # Load datasets associated with the trial.
        bootstrap_datasets_savename = os.path.join(bootstrap_savedir,
                f"future_safety_frac_{frac_data_in_safety:.2f}", "bootstrap_datasets",
                f"bootstrap_datasets_trial_{bootstrap_trial_i}.pkl")
        try:
            bootstrap_datasets_dict = load_pickle(bootstrap_datasets_savename)
        except Exception:
            print(bootstrap_datasets_savename)
            assert(False)

        bs_candidate_dataset = bootstrap_datasets_dict["candidate"]
        bs_safety_dataset = bootstrap_datasets_dict["safety"]

        # Combine loaded candidate and safety dataset to create the full dataset.
        combined_dataset = self.candidate_safety_combine(bs_candidate_dataset, bs_safety_dataset)

        # Set the datasets associated with the trial.
        spec_for_bootstrap_trial.dataset = combined_dataset 
        spec_for_bootstrap_trial.candidate_dataset = bs_candidate_dataset
        spec_for_bootstrap_trial.safety_dataset = bs_safety_dataset
        spec_for_bootstrap_trial.frac_data_in_safety = frac_data_in_safety

        # Update spec with hyperparam_setting.
        spec_for_bootstrap_trial = self.set_spec_with_hyperparam_setting(
                spec_for_bootstrap_trial, hyperparam_setting)

        return spec_for_bootstrap_trial


    def run_bootstrap_trial(
            self,
            bootstrap_trial_i,
            **kwargs
    ):
        """Run bootstrap train bootstrap_trial_i to estimate the probability of passing
        with est_frac_data_in_safety.

        Returns a boolean indicating if the bootstrap trial was actually run. If the 
            bootstrap has been already run, will return False.

        :param bootstrap_trial_i: integer indicating which trial of the bootstrap 
            experiment we are currently running. Allows us to identify which bootstrapped
            dataset to load adn run
        :type bootstrap_trial_i: int
        :param est_frac_data_in_safety: fraction of data in safety set that we want to
            estimate the probabiilty of returning a solution for
        :type est_frac_data_in_safety: float
        :param bootstrap_savedir: The root diretory to load bootstrapped dataset and save 
            the result of this bootstrap trial
        :type bootstrap_savedir: str
        """
        # TODO: Update this with the other kwargs, should be a spec.
        est_frac_data_in_safety = kwargs["est_frac_data_in_safety"]
        bootstrap_savedir = kwargs["bootstrap_savedir"]
        hyperparam_setting = kwargs["hyperparam_setting"]

        # TODO: Update this so that path is passed in further down.
        # Paths to load datasets and store results.
        bs_datasets_savename = os.path.join(bootstrap_savedir,
                f"future_safety_frac_{est_frac_data_in_safety:.2f}", "bootstrap_datasets",
                f"bootstrap_datasets_trial_{bootstrap_trial_i}.pkl")
        bs_result_subdir = os.path.join(bootstrap_savedir,
                f"future_safety_frac_{est_frac_data_in_safety:.2f}", "bootstrap_results")
        bs_result_savename = os.path.join(bs_result_subdir, 
                f"trial_{bootstrap_trial_i}_result.pkl")

        # If this bootstrap trial has already been run, skip.
        if os.path.exists(bs_result_savename):
            if self.spec.verbose:
                print(f"Bootstrap trial {bootstrap_trial_i} has already been run. Skipping.")
            return False

        # Create spec for the bootstrap trial. The bootstrapped candidate and safety
        # datasets are created here.
        # TODO: Update this so that it is actually passed in
        spec_for_bootstrap_trial = self.create_bootstrap_trial_spec(bootstrap_trial_i,
                est_frac_data_in_safety, bootstrap_savedir)

        # Run Seldonian Algorithm on the bootstrapped data. Load the datasets here.
        SA = SeldonianAlgorithm(spec_for_bootstrap_trial)
        try:
            passed_safety, solution = SA.run(write_cs_logfile=self.spec.verbose, 
                    debug=self.spec.verbose)
        except (ValueError, ZeroDivisionError): # Now, all experiemnts should return.
            passed_safety = False
            solution = "NSF"

        # Save the results.
        os.makedirs(bs_result_subdir, exist_ok=True) 
        trial_result_dict = {
                "bootstrap_trial_i" : bootstrap_trial_i,
                "passed_safety" : passed_safety,
                "solution" : solution
        }
        with open(bs_result_savename, "wb") as outfile:
            pickle.dump(trial_result_dict, outfile)
            if self.spec.verbose:
                print(f"Saved results for bootstrap trial {bootstrap_trial_i} for rho' "
                        f"{est_frac_data_in_safety:.2f}")

        # Delete the pickled bootstrap datasets. Don't need anymore
        if os.path.exists(bs_datasets_savename):
            os.remove(bs_datasets_savename)

        return True


    def ttest_bound(
            self,
            bootstrap_trial_data,
            delta=0.1
    ):
        """
        Compute ttest bound on the probability of passing using the bootstrap data across
            bootstrap trials.

        :param bootstrap_trial_data: Array of size n_bootstrap_samples, containing the 
            result of each bootstrap trial.
        :type bootstrap_trial_data: np.array
        :param delta: confidence level, i.e. 0.05
        :type delta: float
        """
        bs_data_mean = np.nanmean(bootstrap_trial_data) # estimated probability of passing
        bs_data_stddev = np.nanstd(bootstrap_trial_data)

        lower_bound = bs_data_mean - bs_data_stddev / np.sqrt(
                self.hyperparam_spec.n_bootstrap_trials)  * tinv(
                        1.0 - delta, self.hyperparam_spec.n_bootstrap_trials - 1)
        upper_bound = bs_data_mean + bs_data_stddev / np.sqrt(
                self.hyperparam_spec.n_bootstrap_trials) * tinv(
                1.0 - delta, self.hyperparam_spec.n_bootstrap_trials - 1)

        return lower_bound, upper_bound


    def clopper_pearson_bound(
            self,
            pass_count, 
            alpha=0.1, # Acceptable error
    ):
        # TODO: Write tests.
        # Update this to work here.
        """
        Computes a 1-alpha clopper pearson bound on the probability of passing. 

        :param pass_count: number of trials out of n_bootstrap_trials that passed
        :type pass_count: int
        :param alpha: confidence parameter
        :type alpha : float
        """
        lower_bound = scipy.stats.beta.ppf(alpha/2, pass_count, 
                self.hyperparam_spec.n_bootstrap_trials - pass_count + 1)
        upper_bound = scipy.stats.beta.ppf(1 - alpha/2, pass_count+ 1, 
                self.hyperparam_spec.n_bootstrap_trials - pass_count)

        return lower_bound, upper_bound


    def aggregate_est_prob_pass(
            self,
            est_frac_data_in_safety,
            bootstrap_savedir
    ):
        """Compute the estimated probability of passing using the result files in 
        bootstrap_savedir.

        :param est_frac_data_in_safety: fraction of data in safety set that we want to 
                        estimate the probabiilty of returning a solution for
        :type est_frac_data_in_safety: float
        :param bootstrap_savedir: root diretory to load results from bootstrap trial, and
            write aggregated result
        :type bootstrap_savedir: str
        """
        # TODO: Update this to allow aggregating first n, for given n.
        bs_frac_subdir = os.path.join(bootstrap_savedir,
                f"future_safety_frac_{est_frac_data_in_safety:.2f}")
        bs_result_subdir = os.path.join(bs_frac_subdir, "bootstrap_results")

        # Load the result for each trial.
        bs_trials_index = []
        bs_trials_pass = []
        bs_trials_solution = []
        for result_trial_savename in os.listdir(bs_result_subdir):
            try:
                result_trial_dict = load_pickle(
                        os.path.join(bs_result_subdir, result_trial_savename))
            except Exception:
                print(os.path.join(bs_result_subdir, result_trial_savename))
                assert(False)
            bs_trials_index.append(result_trial_dict["bootstrap_trial_i"])
            bs_trials_pass.append(result_trial_dict["passed_safety"])
            bs_trials_solution.append(result_trial_dict["solution"])

        # Create dataframe containing data.
        results_df = pd.DataFrame(data = {
            "passed_safety" : bs_trials_pass,
            "solution" : bs_trials_solution
        })
        results_df.index = bs_trials_index
        results_df.sort_index(inplace=True)
        results_csv_savename = os.path.join(bs_frac_subdir, "all_bs_trials_results.csv")
        results_df.to_csv(results_csv_savename)

        # Compute the probability of passing.
        # TODO: When is this nan? Should we change to nan mean?
        est_prob_pass = np.mean(bs_trials_pass)
        num_trials_passed = np.sum(bs_trials_pass)

        # TODO: Update so delta is passed through to CIs.
        if self.hyperparam_spec.confidence_interval_type == "ttest":
            lower_bound, upper_bound = self.ttest_bound(bs_trials_pass)
        elif self.hyperparam_spec.confidence_interval_type == "clopper-pearson":
            lower_bound, upper_bound = self.clopper_pearson_bound(num_trials_passed)
        else:
            lower_bound, upper_bound = None, None

        # TODO: Update tests to have these returns.
        return est_prob_pass, lower_bound, upper_bound, results_df


    def get_bootstrap_dataset_size(
            self,
            frac_data_in_safety
    ):
        """Computes the number of datapoints that should go into the bootstrapped 
                candidate and safety datasets according to frac_data_in_safety.

        :param frac_data_in_safety: fraction of data in safety set that we want to estimate
                        the probabiilty of returning a solution for
        :type frac_data_in_safety: float
        """
        total_data = self.dataset.num_datapoints # Original dataset size.
        n_bootstrap_samples_safety = int(total_data * frac_data_in_safety) 
        n_bootstrap_samples_candidate = total_data - n_bootstrap_samples_safety

        return n_bootstrap_samples_candidate, n_bootstrap_samples_safety


    def get_est_prob_pass(
        self,
        est_frac_data_in_safety,
        candidate_dataset,
        n_bootstrap_samples_candidate,
        n_bootstrap_samples_safety,
        bootstrap_savedir,
        hyperparam_setting=None
    ):
        """Estimates probability of returning a solution with rho_prime fraction of data
            in candidate selection.

        :param est_frac_data_in_safety: fraction of data in safety set that we want to
            estimate the probabiilty of returning a solution for
        :type est_frac_data_in_safety: float
        :param candidate_dataset: a dataset object containing candidate solution dataset.
            This is the dataset we will bootstrap sample from to compute estimate.
        :type candidate_dataset: :py:class:`.DataSet` object
        :param n_bootstrap_samples_candidate: size of candidate dataset sampled in bootstrap
        :type n_boostrap_samples_candidate: int
        :param n_bootstrap_samples_safety: size of safety dataset sampled in bootstrap
        :type n_bootstrap_samples_safety: int
        :param bootstrap_savedir: root diretory to store bootstrap datasets and results
        :type bootstrap_savedir: str
        :type hyperparam_setting: tuple containing hyperparameter values that should be
            set for this bootstrap experiment (if not given will use default from self.spec)
        :type hyperparam_setting: tuple of tuples, where each inner tuple takes the form
            (hyperparameter name, hyperparameter type, hyperparameter value)
                Example:
                (("alpha_theta", "optimization", 0.001), ("num_iters", "optimization", 500))
        """
        # Generate the bootstrapped datsets to use across all trials.
        # TODO: Do we need to think about bootstrap dataset generation in any way for hyperparameters?
        created_trial_datasets = self.generate_all_bootstrap_datasets(
                candidate_dataset, est_frac_data_in_safety, n_bootstrap_samples_candidate, 
                n_bootstrap_samples_safety, bootstrap_savedir)
        # TODO: Log created_trial_datasets

        # Create a partial function for run_bootstrap_trial.
        partial_kwargs = { 
                "est_frac_data_in_safety": est_frac_data_in_safety,
                "bootstrap_savedir": bootstrap_savedir,
                "hyperparam_setting": hyperparam_setting
                }
        helper = partial(self.run_bootstrap_trial, **partial_kwargs)

        # Run the trials.
        bs_trials_ran = [] # List indicating if the bootstrap trial was run.
        start_time = time.time()
        if self.hyperparam_spec.n_bootstrap_workers == 1:
            for bootstrap_trial_i in tqdm(range(self.hyperparam_spec.n_bootstrap_trials), leave=False):
                # TODO: Log bs_trials_run.
                bs_trials_ran.append(helper(bootstrap_trial_i))
        elif self.hyperparam_spec.n_bootstrap_workers > 1: 
            with ProcessPoolExecutor(
                    max_workers=self.hyperparam_spec.n_bootstrap_workers, mp_context=mp.get_context("fork")
            ) as ex:
                for ran_trial in tqdm(
                        ex.map(helper, np.arange(self.hyperparam_spec.n_bootstrap_trials)),
                        total=self.hyperparam_spec.n_bootstrap_trials, leave=False):
                    bs_trials_ran.append(ran_trial)
        else:
            raise ValueError(f"n_workers value of {self.hyperparam_spec.n_bootstrap_workers} must be >=1")
        elapsed_time = time.time() - start_time

        # If trial was run, we want to indicate that at least one trial was run.
        ran_new_bs_trials = any(bs_trials_ran)

        # Accumulate results from bootstrap trials get estimate.
        est_prob_pass, lower_bound, upper_bound, results_df = \
                self.aggregate_est_prob_pass(
                        est_frac_data_in_safety, bootstrap_savedir)

        # TODO: Update the test for including lower and upper bounds.
        return (est_prob_pass, lower_bound, upper_bound, results_df, elapsed_time, 
                ran_new_bs_trials)


    def get_all_greater_est_prob_pass(
            self,
        ):
        """Compute the estimated probability of passing for all safety fractions in  
            self.all_frac_data_in_safety.
        """
        start_time = time.time()
        all_estimates = {}
        for frac_data_in_safety in self.all_frac_data_in_safety:  
            print("rho:", frac_data_in_safety)
            all_estimates[frac_data_in_safety] = {}
            bootstrap_savedir = os.path.join(self.results_dir,
                    f"bootstrap_safety_frac_{frac_data_in_safety:.2f}")

            # Partition data according to frac_data_in_safety.
            (   candidate_dataset,
                safety_dataset,
            ) = self.create_dataset(self.dataset, frac_data_in_safety, shuffle=False)

            # Need at least 4 points in candidate to bootstrap estimate.
            if candidate_dataset.num_datapoints < 4: 
                continue

            # Estimate the probability of passing for datasplits with more in selection.
            for frac_data_in_safety_prime in self.all_frac_data_in_safety:
                if frac_data_in_safety_prime > frac_data_in_safety:  # Only consider more data in cs, less in safety.
                    continue
                print(" rho':", frac_data_in_safety_prime)

                # Compute desired sizes of the bootstrapped candidate and safety datasets.
                n_bootstrap_samples_candidate, n_bootstrap_samples_safety = \
                    self.get_bootstrap_dataset_size(frac_data_in_safety_prime)

                # Copmute probability of passing.
                prime_prob_pass, _, _, _, _, ran_new_bs_trials = self.get_est_prob_pass(
                    frac_data_in_safety_prime,
                    candidate_dataset,
                    n_bootstrap_samples_candidate,
                    n_bootstrap_samples_safety,
                    bootstrap_savedir,
                )
                print("     prob pass:", prime_prob_pass)
                all_estimates[frac_data_in_safety][frac_data_in_safety_prime] = prime_prob_pass

        elapsed_time = time.time() - start_time
        print("elapsed_time:", elapsed_time)
        return all_estimates, elapsed_time


    def get_hyperparameter_iterator(
            self
    ):
        """
        Create iterator for every combination of hyperparameter values that we want to
            optimize for.

        Note that we do not consider frac_data_in_safety a hyperparameter we optimize
            in combination with other hyperparamters, so it is not included.
        """
        all_hyper_iterables = []

        for (hyper_name, hyper_info) in self.hyperparam_spec.hyper_schema.hyper_dict.items():
            if hyper_name == "frac_data_in_safety": 
                continue
            hyper_values, hyper_type = hyper_info["values"], hyper_info["hyper_type"]
            all_hyper_iterables.append([(hyper_name, hyper_type, value) for value in hyper_values])

        return itertools.product(*all_hyper_iterables)

    def create_hyperparam_bootstrap_savedir(
            self,
            hyperparam_setting
    ):
        # TODO: Do we want to generalize to make work for safety_frac as well?
        bootstrap_savedir = "bootstrap"
        for (hyper_name, hyper_type, hyper_value) in hyperparam_setting:
            bootstrap_savedir += f"__{hyper_name}_{hyper_value:.2e}"

        return bootstrap_savedir


    def find_best_hyperparameters(
            self,
            frac_data_in_safety
    ):
        # Partition data according to frac_data_in_safety
        (   candidate_dataset,
            safety_dataset,
        ) = self.create_dataset(self.dataset, frac_data_in_safety, shuffle=False)

        # Dictionary mapping hyperparameter setting to the estimated probability.
        all_est_prob_pass = {}

        # Try all hyperparameter combinations.
        # TODO: This is where we would use CMA-ES
        for hyperparam_setting in self.get_hyperparameter_iterator():
            bootstrap_savedir = self.create_hyperparam_bootstrap_savedir(hyperparam_setting)
            os.makedirs(bootstrap_savedir, exist_ok=True)

            # Compute desired sizes of the bootstrapped candiate and safety datasets.
            n_bootstrap_samples_candidate, n_bootstrap_samples_safety = \
                    self.get_bootstrap_dataset_size(frac_data_in_safety)

            # Estimate probability of passing.
            curr_prob_pass, _, _, _, _, curr_ran_new_bs_trials = self.get_est_prob_pass(
                    frac_data_in_safety,
                    candidate_dataset,
                    n_bootstrap_samples_candidate,
                    n_bootstrap_samples_safety,
                    bootstrap_savedir,
                    hyperparam_setting
            )
            all_est_prob_pass[hyperparam_setting] = curr_prob_pass

        # Select the hyperparameter with the highest predicited probability of passing.
        best_hyperparam_setting = max(all_est_prob_pass, key=lambda k: all_est_prob_pass[k])

        # Set spec with best hyperparameter setting.
        best_hyperparam_spec = self.set_spec_with_hyperparam_setting(
                copy.deepcopy(self.spec), best_hyperparam_setting)

        return best_hyperparam_setting, best_hyperparam_spec


    def find_best_frac_data_in_safety(
            self,
            threshold=0.01 # TODO: Come up with a better name than this.
    ):
        """Find the best frac_data_in_safety to use for the Seldonian algorithm.

        :return: (frac_data_in_safety, candidate_dataset, safety_dataset). frac_data_in_safety
                indicates the percentage of total data that is included in the safety dataset.
                candidate_dataset and safety_dataset are dataset objects containing data from
                elf.dataset split according to frac_data_in_safety
        :rtyle: Tuple
        """
        # TODO: Can we pass in frac_data_in_safety now as a hyperparam_setting? Just a single one?
        # TODO: Update test now that use CI.
        all_est_dict_list = [] # Store dicionaries for dataframe.
        ran_new_bs_trials = False 

        # Move data from safety to cs dataset, high high to low. self.all_frac_data_in_safety
        #   was sorted in init.
        for frac_data_in_safety in self.all_frac_data_in_safety:  
            bootstrap_savedir = os.path.join(self.results_dir, 
                    f"bootstrap_safety_frac_{frac_data_in_safety:.2f}")
            os.makedirs(bootstrap_savedir, exist_ok=True) 

            # Partition data according to frac_data_in_safety.
            (   candidate_dataset,
                safety_dataset,
            ) = self.create_dataset(self.dataset, frac_data_in_safety, shuffle=False)

            # Dictionary mapping est_frac_data_in_safety to the estimated probability.
            all_est_prob_pass = {}

            # Need at least 4 points in candidate to estimate probability of other splits.
            if candidate_dataset.num_datapoints < 4: 
                continue

            # Compute desired sizes of the bootstrapped candidate and safety datasets.
            n_bootstrap_samples_candidate, n_bootstrap_samples_safety = \
                self.get_bootstrap_dataset_size(frac_data_in_safety)

            # Estimate probability of passing.
            curr_prob_pass, curr_lower_bound, curr_upper_bound, _, _, curr_ran_new_bs_trials \
                    = self.get_est_prob_pass(
                            frac_data_in_safety,
                            candidate_dataset,
                            n_bootstrap_samples_candidate,
                            n_bootstrap_samples_safety,
                            bootstrap_savedir,
                        )  
            ran_new_bs_trials = ran_new_bs_trials or curr_ran_new_bs_trials
            all_est_prob_pass[frac_data_in_safety] = curr_prob_pass
            all_est_dict_list.append({
                "frac_data_in_safety": frac_data_in_safety, 
                "est_frac_data_in_safety": frac_data_in_safety,
                "est_prob_pass": curr_prob_pass,
                "est_lower_bound": curr_lower_bound,
                "est_upper_bound": curr_upper_bound,
            })

            # Estimate if any of the future splits of data lead to higher P(pass)
            prime_better = False
            all_prime_below_threshold = False
            for est_frac_data_in_safety in self.all_frac_data_in_safety:
                if est_frac_data_in_safety >= frac_data_in_safety:  # Est if more data in cs.
                    continue

                # Compute desired sizes of the bootstrapped candidate and safety datasets.
                n_bootstrap_samples_candidate, n_bootstrap_samples_safety = \
                    self.get_bootstrap_dataset_size(est_frac_data_in_safety)

                # Compute probability of passing.
                prime_prob_pass, prime_lower_bound, prime_upper_bound, _, _, curr_ran_new_bs_trials \
                        = self.get_est_prob_pass(
                                est_frac_data_in_safety,
                                candidate_dataset,
                                n_bootstrap_samples_candidate,
                                n_bootstrap_samples_safety,
                                bootstrap_savedir,
                            )
                ran_new_bs_trials = ran_new_bs_trials or curr_ran_new_bs_trials
                all_est_prob_pass[est_frac_data_in_safety] = prime_prob_pass
                all_est_dict_list.append({
                    "frac_data_in_safety": frac_data_in_safety, 
                    "est_frac_data_in_safety": est_frac_data_in_safety,
                    "est_prob_pass": prime_prob_pass,
                    "est_lower_bound": prime_lower_bound,
                    "est_upper_bound": prime_upper_bound,
                })

                # Check if estimate is below threshold.
                all_prime_below_threshold = all_prime_below_threshold and (prime_prob_pass <= threshold)

                # Check if ound a future split that we predict is better.
                if self.hyperparam_spec.confidence_interval_type is not None: # Compare confidence intervals.
                    if prime_upper_bound >= curr_lower_bound:
                        prime_better = True
                        break
                else: # Use point estimate to compare.
                    if prime_prob_pass >= curr_prob_pass:
                        prime_better = True
                        break

            # We do not predict that moving more data into the candidate selection is better,
            # so use current rho.
            if not prime_better and not all_prime_below_threshold:
                break

        # Write out all the estimates to a dataframe.
        all_est_csv_savename = os.path.join(self.results_dir, "all_bootstrap_est.csv")
        all_est_df = pd.DataFrame(all_est_dict_list)
        all_est_df.to_csv(all_est_csv_savename)

        # TODO: frac_data_in_safety should be saved... but perhaps at the top level
        return (frac_data_in_safety, candidate_dataset, safety_dataset, ran_new_bs_trials)

