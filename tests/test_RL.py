import pytest
from seldonian.RL.Agents.Agent import Agent
from seldonian.RL.Agents.Function_Approximators.Table import *
from seldonian.RL.Agents.Policies.Policy import *
from seldonian.RL.Agents.Policies.SimglucosePolicyFixedArea import (
    SigmoidPolicyFixedArea,
)
from seldonian.RL.Agents.Policies.Softmax import *
from seldonian.RL.environments.mountaincar import Mountaincar
from seldonian.RL.environments.n_step_mountaincar import N_step_mountaincar
from seldonian.RL.environments.gridworld import Gridworld
from seldonian.RL.Agents.Parameterized_non_learning_softmax_agent import *
from seldonian.RL.Agents.Discrete_Random_Agent import *
from seldonian.RL.RL_runner import run_trial
from seldonian.dataset import RLDataSet, RLMetaData
import autograd.numpy as np


def test_Qtable():
    """tests Table class methods"""

    min_state = -3
    num_states = 6
    num_actions = 3
    mytable = Q_Table(min_state, num_states, num_actions)

    # check from_environment_state_to_0_indexed_state()
    assert mytable.from_environment_state_to_0_indexed_state(-3) == 0
    assert mytable.from_environment_state_to_0_indexed_state(2) == 5

    # check initialization
    assert np.allclose(
        mytable.get_action_values_given_state(-3), np.array([0.0, 0.0, 0.0])
    )

    # check manually setting weights, and get_action_values_given_state()
    mytable.weights[0, 0] = 1.1
    mytable.weights[0, 1] = 2.2
    mytable.weights[0, 2] = -3.3
    assert np.allclose(
        mytable.get_action_values_given_state(-3), np.array([1.1, 2.2, -3.3])
    )
    for state in range(-2, 3):
        assert np.allclose(
            mytable.get_action_values_given_state(state), np.array([0.0, 0.0, 0.0])
        )

    mytable.weights[3, 0] = 1.1
    mytable.weights[3, 1] = 2.2
    mytable.weights[3, 2] = -3.3
    assert np.allclose(
        mytable.get_action_values_given_state(0), np.array([1.1, 2.2, -3.3])
    )


### Policies


def test_policy_base_class():
    obs = 0
    action = 0
    observations = [0, 1, 2, 3]
    actions = [0, 9, 8, 4]
    policy = Policy()
    behavior_action_probs = [0.99, 0.22, 0.19, 0.4]
    new_params = np.array([1, 2, 3])

    with pytest.raises(NotImplementedError) as excinfo:
        policy.choose_action(obs)
    with pytest.raises(NotImplementedError) as excinfo:
        policy.set_new_params(new_params)
    with pytest.raises(NotImplementedError) as excinfo:
        policy.get_params()
    with pytest.raises(NotImplementedError) as excinfo:
        policy.get_prob_this_action(obs, action)
    with pytest.raises(NotImplementedError) as excinfo:
        policy.get_probs_from_observations_and_actions(
            observations, actions, behavior_action_probs
        )


def test_Discrete_Action_Policy():
    """test Discrete_Action_Policy action index methods"""
    min_action = -1
    max_action = 1
    observation_space = Discrete_Space(-1, 2)  # irrelevant for test
    action_space = Discrete_Space(min_action, max_action)
    env_description = Env_Description(observation_space, action_space)
    hyperparam_and_setting_dict = {}

    p = Discrete_Action_Policy(hyperparam_and_setting_dict, env_description)

    assert p.from_environment_action_to_0_indexed_action(-1) == 0
    assert p.from_environment_action_to_0_indexed_action(0) == 1
    assert p.from_environment_action_to_0_indexed_action(1) == 2

    assert p.from_0_indexed_action_to_environment_action(0) == -1
    assert p.from_0_indexed_action_to_environment_action(1) == 0
    assert p.from_0_indexed_action_to_environment_action(2) == 1


def test_Softmax():
    """test Softmax functions"""
    min_action = -1
    max_action = 1
    observation_space = Discrete_Space(-1, 2)
    action_space = Discrete_Space(min_action, max_action)
    env_description = Env_Description(observation_space, action_space)
    hyperparam_and_setting_dict = {}

    sm = Softmax(hyperparam_and_setting_dict, env_description)
    e_to_something_stable = np.array([0.1108031584, 0.0040867714, 1.0])
    assert np.allclose(
        sm.get_e_to_the_something_terms([1.1, -2.2, 3.3]), e_to_something_stable
    )
    assert np.allclose(
        sm.get_action_probs_from_action_values([1.1, -2.2, 3.3]),
        e_to_something_stable / sum(e_to_something_stable),
    )
    assert sm.get_prob_this_action(0, 0) == 1 / 3
    assert sm.get_prob_this_action(0, 1) == 1 / 3
    assert sm.get_prob_this_action(-1, 0) == 1 / 3
    assert sm.get_prob_this_action(1, -1) == 1 / 3

    # Wrong number of action values results in error
    with pytest.raises(Exception) as excinfo:
        sm.choose_action_from_action_values([0, 1, 2, 3, 4, 5])
    error_str = f"should have {sm.num_actions} actions, but got 6 action values"

    assert str(excinfo.value) == error_str

    # Get probabilites from list of observations and actions
    observations = [0, 1]
    actions = [0, 1]
    behavior_action_probs = [0.4, 0.6]
    probs = sm.get_probs_from_observations_and_actions(
        observations, actions, behavior_action_probs
    )
    answer = np.array([0.33333333, 0.33333333])
    assert np.allclose(probs, answer)


def test_MixedSoftmax():
    """test Mixed Softmax (for policy regularization)"""
    min_action = -1
    max_action = 1
    observation_space = Discrete_Space(-1, 2)  # irrelevant for test
    action_space = Discrete_Space(min_action, max_action)
    env_description = Env_Description(observation_space, action_space)
    hyperparam_and_setting_dict = {}

    alpha1 = 1.0  # mixing hyperparam
    sm1 = MixedSoftmax(hyperparam_and_setting_dict, env_description, alpha1)
    e_to_something_stable = np.array([0.1108031584, 0.0040867714, 1.0])
    assert np.allclose(
        sm1.get_e_to_the_something_terms([1.1, -2.2, 3.3]), e_to_something_stable
    )
    assert np.allclose(
        sm1.get_action_probs_from_action_values([1.1, -2.2, 3.3]),
        e_to_something_stable / sum(e_to_something_stable),
    )
    assert sm1.get_prob_this_action(0, 0, 1 / 4) == 1 / 3
    assert sm1.get_prob_this_action(0, 0, 1 / 2) == 1 / 3

    alpha2 = 0.0  # mixing hyperparam
    sm2 = MixedSoftmax(hyperparam_and_setting_dict, env_description, alpha2)
    e_to_something_stable = np.array([0.1108031584, 0.0040867714, 1.0])
    assert np.allclose(
        sm2.get_e_to_the_something_terms([1.1, -2.2, 3.3]), e_to_something_stable
    )
    assert np.allclose(
        sm2.get_action_probs_from_action_values([1.1, -2.2, 3.3]),
        e_to_something_stable / sum(e_to_something_stable),
    )
    assert sm2.get_prob_this_action(0, 0, 1 / 4) == 1 / 4
    assert sm2.get_prob_this_action(0, 0, 1 / 2) == 1 / 2

    alpha3 = 0.5  # mixing hyperparam
    sm3 = MixedSoftmax(hyperparam_and_setting_dict, env_description, alpha3)
    e_to_something_stable = np.array([0.1108031584, 0.0040867714, 1.0])
    assert np.allclose(
        sm3.get_e_to_the_something_terms([1.1, -2.2, 3.3]), e_to_something_stable
    )
    assert np.allclose(
        sm3.get_action_probs_from_action_values([1.1, -2.2, 3.3]),
        e_to_something_stable / sum(e_to_something_stable),
    )
    assert sm3.get_prob_this_action(0, 0, 1 / 4) == 1 / 3 * alpha3 + 1 / 4 * (
        1 - alpha3
    )
    assert sm3.get_prob_this_action(0, 0, 1 / 2) == 1 / 3 * alpha3 + 1 / 2 * (
        1 - alpha3
    )


def test_simglucose_policy():
    bb_crmin = 5.0
    bb_crmax = 15.0
    bb_cfmin = 10.0
    bb_cfmax = 20.0
    cr_shrink_factor = np.sqrt(3)
    cf_shrink_factor = np.sqrt(3)
    policy1 = SigmoidPolicyFixedArea(
        bb_crmin, bb_crmax, bb_cfmin, bb_cfmax, cr_shrink_factor, cf_shrink_factor
    )
    # Set new parameters and check that they were updated
    new_params = np.array([0.0, 5.0])
    policy1.set_new_params(new_params)
    assert np.allclose(policy1.get_params(), new_params)
    # Test that theta2crcf works
    cr1, cr2, cf1, cf2 = policy1.theta2crcf(new_params)
    assert cr1 == pytest.approx(7.113248654051871)
    assert cr2 == pytest.approx(12.886751345948129)
    assert cf1 == pytest.approx(14.198209991688712)
    assert cf2 == pytest.approx(19.97171268358497)

    bb_crmin = 5.0
    bb_crmax = 15.0
    bb_cfmin = 10.0
    bb_cfmax = 20.0
    cr_shrink_factor = 0.5
    cf_shrink_factor = 0.5
    policy2 = SigmoidPolicyFixedArea(
        bb_crmin, bb_crmax, bb_cfmin, bb_cfmax, cr_shrink_factor, cf_shrink_factor
    )
    # Set new parameters and check that they were updated
    new_params = np.array([0.0, 5.0])
    policy2.set_new_params(new_params)
    assert np.allclose(policy2.get_params(), new_params)
    # Test that theta2crcf works
    cr1, cr2, cf1, cf2 = policy2.theta2crcf(new_params)
    assert cr1 == pytest.approx(5)
    assert cr2 == pytest.approx(15)
    assert cf1 == pytest.approx(10)
    assert cf2 == pytest.approx(20)


### Agents


def test_Agent_base_class():
    agent = Agent()
    # Test that all base class methods assert NotImplementedError
    with pytest.raises(NotImplementedError) as excinfo:
        agent.choose_action(0)

    with pytest.raises(NotImplementedError) as excinfo:
        agent.update(0, 1, 0, False)

    with pytest.raises(NotImplementedError) as excinfo:
        agent.get_prob_this_action(0, 1)

    with pytest.raises(NotImplementedError) as excinfo:
        agent.set_new_params(np.array([0, 1, 2]))

    with pytest.raises(NotImplementedError) as excinfo:
        agent.get_params()

    with pytest.raises(NotImplementedError) as excinfo:
        agent.get_policy()


def test_Parameterized_non_learning_softmax_agent():
    """test Parameterized_non_learning_softmax_agent"""
    observation_space = Discrete_Space(-1, 2)
    action_space = Discrete_Space(-1, 1)
    env_desc = Env_Description(observation_space, action_space)
    hyperparam_and_setting_dict = {}
    hyperparam_and_setting_dict["basis"] = "Fourier"
    agent = Parameterized_non_learning_softmax_agent(
        env_desc, hyperparam_and_setting_dict
    )

    correct_shape = (4, 3)
    assert agent.get_params().shape == correct_shape

    new_params = np.random.rand(correct_shape[0], correct_shape[1])
    agent.set_new_params(new_params)
    assert np.allclose(
        agent.get_params(), new_params
    )  # test set_new_params() and get_params()

    # test get_action_values() and get_prob_this_action()
    agent.set_new_params(
        np.array(
            [[1.1, 1.1, 1.1, 1.1], [-2.2, -2.2, -2.2, -2.2], [3.3, 3.3, 3.3, 3.3]]
        ).T
    )
    for state in range(-1, 3):
        assert np.allclose(agent.get_action_values(state), [1.1, -2.2, 3.3])
        assert np.allclose(agent.get_prob_this_action(state, -1), 0.099384841)
        assert np.allclose(agent.get_prob_this_action(state, 0), 0.0036656277)
        assert np.allclose(agent.get_prob_this_action(state, 1), 0.8969495313)

    incorrect_shape = (2, 5)
    bad_params = np.random.rand(incorrect_shape[0], incorrect_shape[1])
    with pytest.raises(Exception):
        agent.set_new_params(bad_params)  # make sure throws error with bad shape

    # Ensure that policy is a softmax
    assert isinstance(agent.get_policy(), Softmax)


def test_Discrete_Random_Agent():
    """test Discrete Random Agent"""
    observation_space = Discrete_Space(-1, 2)
    action_space = Discrete_Space(-1, 1)
    env_desc = Env_Description(observation_space, action_space)

    agent = Discrete_Random_Agent(env_desc)
    # update() is a no-op
    agent.update(observation=0, next_observation=1, reward=1, terminated=False)
    assert agent.num_actions == 3
    correct_shape = (4, 3)
    with pytest.raises(NotImplementedError) as excinfo:
        params = agent.get_params()

    new_params = np.random.rand(correct_shape[0], correct_shape[1])
    with pytest.raises(NotImplementedError) as excinfo:
        agent.set_new_params(new_params)

    # choose action
    action = agent.choose_action(observation=-1)
    assert action in [-1, 0, 1]
    action = agent.choose_action(observation=1e7)
    assert action in [-1, 0, 1]

    # action probabilities
    prob = agent.get_prob_this_action(-1, 0)
    assert prob == 1 / 3
    prob = agent.get_prob_this_action(1e4, 1)
    assert prob == 1 / 3


def test_spaces_and_env_descriptions():
    """test Continuous_Space constructor and methods"""
    cont_space = Continuous_Space(
        np.array([[0.0, 1.1], [3.3, 4.4], [5.5, 6.6]])
    )  # should be no error
    with pytest.raises(Exception):
        cont_space = Continuous_Space(
            np.array([[0.0, 1.1], [3.3, 4.4], [5.5, -6.6]])
        )  # max is smaller than min
    with pytest.raises(Exception):
        cont_space = Continuous_Space(
            np.array([[2.0, 1.1], [3.3, 4.4], [5.5, 6.6]])
        )  # max is smaller than min
    with pytest.raises(Exception):
        cont_space = Continuous_Space(
            np.array([[2.0, 2.1, 3.3], [4.4, 5.5, 6.6]])
        )  # need 2 values, not 3

    obs_space = Discrete_Space(-10, 10)
    assert obs_space.get_num_values() == 21
    action_space = Discrete_Space(1, 4)
    assert action_space.get_num_values() == 4

    env_desc = Env_Description(obs_space, action_space)
    assert env_desc.get_num_states() == 21
    assert env_desc.get_num_actions() == 4
    assert env_desc.get_min_action() == 1
    assert env_desc.get_min_state() == -10


def test_Fourier():
    hyperparam_and_setting_dict = {}
    hyperparam_and_setting_dict["order"] = 2
    hyperparam_and_setting_dict["max_coupled_vars"] = -1
    env = Mountaincar()
    env_desc = env.env_description
    basis = Fourier(hyperparam_and_setting_dict, env_desc)
    assert basis.num_features == 9
    assert np.array_equal(
        basis.basis_matrix,
        np.array(
            [[0, 0], [0, 1], [0, 2], [1, 0], [1, 1], [1, 2], [2, 0], [2, 1], [2, 2]]
        ),
    )

    # Bad order
    hyperparam_and_setting_dict = {}
    hyperparam_and_setting_dict["order"] = 0
    env = Mountaincar()
    env_desc = env.env_description
    with pytest.raises(Exception) as excinfo:
        basis = Fourier(hyperparam_and_setting_dict, env_desc)

    # Bad max_coupled_vars
    hyperparam_and_setting_dict = {}
    hyperparam_and_setting_dict["max_coupled_vars"] = 0
    env = Mountaincar()
    env_desc = env.env_description
    with pytest.raises(Exception) as excinfo:
        basis = Fourier(hyperparam_and_setting_dict, env_desc)

    # max_coupled_vars < number of dimensions
    hyperparam_and_setting_dict = {}
    hyperparam_and_setting_dict["max_coupled_vars"] = 1
    env = Mountaincar()
    env_desc = env.env_description
    with pytest.raises(Exception) as excinfo:
        basis = Fourier(hyperparam_and_setting_dict, env_desc)

    # Calculate number of features

    num_features = basis.calculate_num_features(
        order=2, max_coupled_vars=2, num_obs_dims=3
    )
    assert num_features == 19


def test_createRLSpec_gridworld(RL_gridworld_dataset):
    """Test creating RLSpec object
    for default gridworld inputs"""
    from seldonian.spec import createRLSpec

    constraint_strs = ["J_pi_new_IS >= -0.25"]
    deltas = [0.05]

    (dataset, policy, env_kwargs, primary_objective) = RL_gridworld_dataset()

    spec = createRLSpec(
        dataset=dataset,
        policy=policy,
        env_kwargs=env_kwargs,
        constraint_strs=constraint_strs,
        deltas=deltas,
        save_dir="",
        verbose=False,
    )

    assert spec.model.env_kwargs["gamma"] == 0.9
    assert isinstance(spec.model.policy, Softmax)


def test_createRLSpec_mountaincar(N_step_mountaincar_dataset):
    """Test creating RLSpec object
    for default gridworld inputs"""
    from seldonian.spec import createRLSpec

    constraint_strs = ["J_pi_new_IS >= -500"]
    deltas = [0.05]

    (dataset, policy, env_kwargs, primary_objective) = N_step_mountaincar_dataset()

    spec = createRLSpec(
        dataset=dataset,
        policy=policy,
        env_kwargs=env_kwargs,
        constraint_strs=constraint_strs,
        deltas=deltas,
        save_dir="",
        verbose=False,
    )

    assert spec.model.env_kwargs["gamma"] == 1.0
    assert isinstance(spec.model.policy, Softmax)


def test_generate_gridworld_episodes():
    """Test that we can generate proper episodes for gridworld
    with the behavior policy (uniform random)."""
    hyperparam_and_setting_dict = {}
    hyperparam_and_setting_dict["env"] = Gridworld()
    hyperparam_and_setting_dict["agent"] = "Parameterized_non_learning_softmax_agent"
    hyperparam_and_setting_dict["num_episodes"] = 10
    hyperparam_and_setting_dict["vis"] = False

    episodes = run_trial(hyperparam_and_setting_dict)

    assert len(episodes) == 10
    first_episode = episodes[0]
    observations = first_episode.observations
    actions = first_episode.actions
    rewards = first_episode.rewards
    pis = first_episode.action_probs
    assert len(observations) >= 2
    assert len(actions) >= 2
    assert len(rewards) >= 2
    assert len(pis) >= 2

    first_observation = observations[0]
    assert first_observation == 0
    first_action = actions[0]
    assert first_action in [0, 1, 2, 3]
    first_reward = rewards[0]
    assert first_reward == 0
    assert all([pi == 0.25 for pi in pis])

    meta = RLMetaData(
        all_col_names=["episode_index", "O", "A", "R", "pi_b"], sensitive_col_names=[]
    )
    dataset = RLDataSet(episodes=episodes, meta=meta)
    assert len(dataset.episodes) == 10


def test_generate_n_step_mountaincar_episodes():
    """Test that we can generate proper episodes for n_step_mountaincar
    with the behavior policy (uniform random)."""
    hyperparam_and_setting_dict = {}
    hyperparam_and_setting_dict["env"] = N_step_mountaincar()
    hyperparam_and_setting_dict["agent"] = "Parameterized_non_learning_softmax_agent"
    hyperparam_and_setting_dict["basis"] = "Fourier"
    hyperparam_and_setting_dict["order"] = 2
    hyperparam_and_setting_dict["max_coupled_vars"] = -1
    hyperparam_and_setting_dict["num_episodes"] = 10
    hyperparam_and_setting_dict["vis"] = False

    episodes = run_trial(hyperparam_and_setting_dict)

    assert len(episodes) == 10
    first_episode = episodes[0]
    observations = first_episode.observations
    actions = first_episode.actions
    rewards = first_episode.rewards
    pis = first_episode.action_probs
    assert len(observations) >= 2
    assert len(actions) >= 2
    assert len(rewards) >= 2
    assert len(pis) >= 2

    first_observation = observations[0]
    assert np.allclose(first_observation, np.array([-0.5, 0.0]))
    first_action = actions[0]
    assert first_action in [-1, 0, 1]
    first_reward = rewards[0]
    assert first_reward == -20.0
    assert all([pi == 1 / 3.0 for pi in pis])

    meta = RLMetaData(
        all_col_names=["episode_index", "O", "A", "R", "pi_b"], sensitive_col_names=[]
    )
    dataset = RLDataSet(episodes=episodes, meta=meta)
    assert len(dataset.episodes) == 10
