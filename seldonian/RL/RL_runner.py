from seldonian.RL.Agents.Discrete_Random_Agent import *
from seldonian.RL.Agents.mountain_car_rough_solution import *
from seldonian.RL.Agents.Parameterized_non_learning_softmax_agent import *
from seldonian.RL.Agents.keyboard_gridworld import *

from seldonian.RL.environments.gridworld import *
from seldonian.RL.environments.mountaincar import *
from seldonian.RL.environments.n_step_mountaincar import *
from seldonian.RL.environments.simglucose_env import *

from seldonian.dataset import Episode

def run_all_trials(hyperparameter_and_setting_dict):
    """ Run many trials, each of which consist
    of the same number of episodes.
    
    :param hyperparameter_and_setting_dict: Specifies the
        environment, agent, number of episodes per trial,
        and number of trials
    :type hyperparameter_and_setting_dict: dict

    :return: List((List of episodes, agent)_i) for i trials
    """
    num_trials = hyperparameter_and_setting_dict["num_trials"]
    trials = []
    for trial_num in range(num_trials):
        trials.append(run_trial(hyperparameter_and_setting_dict)[0])
    return trials

def run_trial(hyperparameter_and_setting_dict):
    """ Run a single trial consists of an arbitrary number of episodes.
    
    :param hyperparameter_and_setting_dict: Specifies the
        environment, agent and number of episodes to run
    :type hyperparameter_and_setting_dict: dict

    :return: (List of episodes, agent)
    """
    agent = create_agent(hyperparameter_and_setting_dict)
    env = create_env(hyperparameter_and_setting_dict)
    episodes = []

    if hyperparameter_and_setting_dict["vis"]:
        env.start_visualizing()
    for episode_num in range(hyperparameter_and_setting_dict["num_episodes"]):
        episodes.append(run_episode(agent, env))
    return episodes, agent

def run_trial_given_agent_and_env(agent,env,num_episodes):
    """ A wrapper for run_trial() where parameters 
    are specified explicity rather than via a dictionary.

    :param agent: RL Agent 

    :param env: RL Environment

    :param num_episodes: Number of episodes to run

    :return: List of episodes
    """
    episodes = []

    for episode_num in range(num_episodes):
        episodes.append(run_episode(agent, env))
    return episodes

def run_episode(agent, env):
    """ Run a single episode 

    :param agent: RL Agent 

    :param env: RL Environment 

    :return: RL Episode
    :rtype: :py:class:`.Episode`
    """
    observations = []
    actions = []
    rewards = []
    prob_actions = []

    env.reset()
    observation = env.get_observation()
    episodic_return = 0.0
    while not env.terminated():
        action = agent.choose_action(observation)
        reward = env.transition(action)
        episodic_return += reward
        next_observation = env.get_observation()
        agent.update(observation, next_observation, reward, env.terminated())

        observations.append(observation)
        actions.append(action)
        rewards.append(reward)
        prob_actions.append(agent.get_prob_this_action(observation, action))

        observation = next_observation

    return Episode(observations, actions, rewards, prob_actions)

def create_agent(hyperparameter_and_setting_dict):
    """ Create an agent from a dictionary specification
    
    :param hyperparameter_and_setting_dict: Specifies the
        environment, agent, number of episodes per trial,
        and number of trials

    :return: RL agent
    :rtype: :py:class:`.Agents.Agent`
    """
    sample_env = create_env(hyperparameter_and_setting_dict)
    env_desc = sample_env.get_env_description()
    agent_type = hyperparameter_and_setting_dict["agent"]
    if agent_type == "discrete_random":
        return Discrete_Random_Agent(env_desc)
    elif agent_type == "mountain_car_rough_solution":
        return Mountain_car_rough_solution()
    elif agent_type == "Parameterized_non_learning_softmax_agent":
        return Parameterized_non_learning_softmax_agent(env_desc, hyperparameter_and_setting_dict)
    elif agent_type == "Keyboard_gridworld":
        return Keyboard_gridworld(env_desc)
    else:
        raise Exception(f"unknown agent type {agent_type}")

def create_env(hyperparameter_and_setting_dict):
    """ Create an environment from a dictionary specification
    
    :param hyperparameter_and_setting_dict: Specifies the
        environment, agent, number of episodes per trial,
        and number of trials

    :return: RL environment
    :rtype: :py:class:`.environments.Environment`
    """
    env_type = hyperparameter_and_setting_dict["env"]
    if env_type == "gridworld":
        return Gridworld()
    elif env_type == "mountaincar":
        return Mountaincar()
    elif env_type == "n_step_mountaincar":
        return N_step_mountaincar()
    elif env_type == "simglucose":
        return Simglucose()
    else:
        raise Exception(f"unknown agent type {env_type}")