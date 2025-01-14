import os
import time
from dataclasses import dataclass
from typing import List, Dict, Callable
from datetime import datetime

import torch
import numpy as np
import pandas as pd
import torch.nn as nn

from src.model.dqn import DQNValueInfo, DQNValue
from src.env import Environment
from src.api.simulator import Simulator
from src.api.testbed import Testbed
from src.api.testbed_simulator import TestbedSimulator
from src.memory.replay import ReplayMemory
from src.dataType import State, Action, Scene
from src.utils import (
    DebugInfo,
    print_debug_info,
    get_device,
    save_animation,
    get_zero_util_cnt,
    get_sfc_cnt_in_same_srv,
    get_possible_actions,
    convert_state_to_vnf_selection_input,
    convert_state_to_vnf_placement_input,
)
from src.const import VNF_PLACEMENT_IN_DIM_WITHOUT_SFC_NUM, VNF_SELECTION_IN_DIM_WITHOUT_SFC_NUM, MAXIMUM_SFC_NUM

DEFAULT_RESULT_PATH_PREFIX = 'result/dqn/'
DEFAULT_PARAMETER_PATH_PREFIX = 'param/dqn/'

os.makedirs(DEFAULT_RESULT_PATH_PREFIX, exist_ok=True)
os.makedirs(DEFAULT_PARAMETER_PATH_PREFIX, exist_ok=True)

@dataclass
class DQNAgentInfo:
    srv_n: int
    sfc_n: int
    max_vnf_num: int
    init_epsilon: float
    final_epsilon: float
    vnf_s_lr: float
    vnf_p_lr: float
    gamma: float
    vnf_s_model_info: DQNValueInfo
    vnf_p_model_info: DQNValueInfo
    edge_name: str


class DQNAgent:
    MAX_MEMORY_LEN = 1_000
    BATCH_SIZE = 32

    def __init__(self, info: DQNAgentInfo) -> None:
        self.info = info
        self.device = info.vnf_s_model_info.device

        self.memory = ReplayMemory(self.BATCH_SIZE, self.MAX_MEMORY_LEN)

        self.vnf_selection_model = DQNValue(info.vnf_s_model_info)
        self.vnf_placement_model = DQNValue(info.vnf_p_model_info)
        self.vnf_selection_optimizer = torch.optim.Adam(
            self.vnf_selection_model.parameters(), lr=info.vnf_s_lr)
        self.vnf_placement_optimzer = torch.optim.Adam(
            self.vnf_placement_model.parameters(), lr=info.vnf_p_lr)
        self.loss_fn = nn.HuberLoss()
        self.edge_name = info.edge_name

    def get_exploration_rate(self, epsilon_sub: float) -> float:
        return max(self.info.final_epsilon, self.info.init_epsilon - epsilon_sub)

    def decide_action(self, state: State, epsilon_sub: float) -> Action:
        possible_actions = get_possible_actions(state, self.info.max_vnf_num)
        vnf_s_in = convert_state_to_vnf_selection_input(
            state, self.info.max_vnf_num)
        epsilon = self.get_exploration_rate(epsilon_sub)
        is_random = np.random.uniform() < epsilon
        if is_random:
            vnf_idxs = []
            for i in range(len(state.vnfs)):
                if len(possible_actions[i]) > 0:
                    vnf_idxs.append(i)
            vnf_s_out = torch.tensor(np.random.choice(vnf_idxs, 1))
        else:
            self.vnf_selection_model.eval()
            with torch.no_grad():
                vnf_s_out = self.vnf_selection_model(vnf_s_in.unsqueeze(0))
                vnf_s_out = vnf_s_out + \
                    torch.tensor([0 if len(possible_actions[i]) > 0 else -
                                 torch.inf for i in range(len(possible_actions))]).to(self.device)
                vnf_s_out = vnf_s_out.max(1)[1]
        vnf_p_in = convert_state_to_vnf_placement_input(state, int(vnf_s_out))
        if is_random:
            srv_idxs = []
            for i in range(len(state.srvs)):
                if i in possible_actions[int(vnf_s_out)]:
                    srv_idxs.append(i)
            vnf_p_out = torch.tensor(np.random.choice(srv_idxs, 1))
        else:
            self.vnf_placement_model.eval()
            with torch.no_grad():
                vnf_p_out = self.vnf_placement_model(vnf_p_in.unsqueeze(0))
                vnf_p_out = vnf_p_out + torch.tensor([0 if i in possible_actions[int(
                    vnf_s_out)] else -torch.inf for i in range(len(state.srvs))]).to(self.device)
                vnf_p_out = vnf_p_out.max(1)[1]
        scene = Scene(
            vnf_s_in=vnf_s_in,
            vnf_s_out=vnf_s_out,
            vnf_p_in=vnf_p_in,
            vnf_p_out=vnf_p_out,
            reward=None,  # this data will get from the env
            next_vnf_p_in=None,  # this data will get from the env
            next_vnf_s_in=None,  # this data will get from the env
        )

        if len(self.memory) > 0:
            self.memory.buffer[-1].next_vnf_s_in = vnf_s_in
            self.memory.buffer[-1].next_vnf_p_in = vnf_p_in
        self.memory.append(scene)
        return Action(
            vnf_id=int(vnf_s_out),
            srv_id=int(vnf_p_out),
        )

    def update(self, _state, _action, reward, _next_state) -> None:
        self.memory.buffer[-1].reward = reward
        # sample a minibatch from memory
        scene_batch = self.memory.sample()
        if len(scene_batch) < self.BATCH_SIZE:
            return
        vnf_s_in_batch = torch.stack(
            [scene.vnf_s_in for scene in scene_batch]).to(self.device)
        vnf_s_out_batch = torch.tensor(
            [scene.vnf_s_out for scene in scene_batch], dtype=torch.int64).unsqueeze(1).to(self.device)
        vnf_p_in_batch = torch.stack(
            [scene.vnf_p_in for scene in scene_batch]).to(self.device)
        vnf_p_out_batch = torch.tensor(
            [scene.vnf_p_out for scene in scene_batch], dtype=torch.int64).unsqueeze(1).to(self.device)
        reward_batch = torch.tensor(
            [scene.reward for scene in scene_batch]).unsqueeze(1).to(self.device)
        next_vnf_s_in_batch = torch.stack(
            [scene.next_vnf_s_in for scene in scene_batch]).to(self.device)
        next_vnf_p_in_batch = torch.stack(
            [scene.next_vnf_p_in for scene in scene_batch]).to(self.device)

        # set model to eval mode
        self.vnf_selection_model.eval()
        self.vnf_placement_model.eval()
        # get state-action value
        vnf_selection_q = self.vnf_selection_model(
            vnf_s_in_batch).gather(1, vnf_s_out_batch)
        vnf_placement_q = self.vnf_placement_model(
            vnf_p_in_batch).gather(1, vnf_p_out_batch)

        # calculate next_state-max_action value
        vnf_selection_expect_q = reward_batch + self.info.gamma * \
            self.vnf_selection_model(next_vnf_s_in_batch).max(1)[
                0].detach().unsqueeze(1)
        vnf_placement_expect_q = reward_batch + self.info.gamma * \
            self.vnf_placement_model(next_vnf_p_in_batch).max(1)[
                0].detach().unsqueeze(1)

        # set model to train mode
        self.vnf_selection_model.train()

        # loss = distance between state-action value and next_state-max-action * gamma + reward
        vnf_selection_loss = self.loss_fn(
            vnf_selection_q, vnf_selection_expect_q)

        # update model
        self.vnf_selection_optimizer.zero_grad()
        vnf_selection_loss.backward()
        self.vnf_selection_optimizer.step()

        # set model to train mode
        self.vnf_placement_model.train()

        # loss = distance between state-action value and next_state-max-action * gamma + reward
        vnf_placement_loss = self.loss_fn(
            vnf_placement_q, vnf_placement_expect_q)

        # update model
        self.vnf_placement_optimzer.zero_grad()
        vnf_placement_loss.backward()
        self.vnf_placement_optimzer.step()

    def save(self) -> None:
        torch.save(self.vnf_selection_model.state_dict(), f'{DEFAULT_PARAMETER_PATH_PREFIX}{self.edge_name}-{self.info.max_vnf_num}_vnf_selection_model.pth')
        torch.save(self.vnf_placement_model.state_dict(), f'{DEFAULT_PARAMETER_PATH_PREFIX}{self.edge_name}-{self.info.max_vnf_num}_vnf_placement_model.pth')

    def load(self) -> None:
        self.vnf_selection_model.load_state_dict(torch.load(f'{DEFAULT_PARAMETER_PATH_PREFIX}{self.edge_name}-{self.info.max_vnf_num}_vnf_selection_model.pth'))
        self.vnf_selection_model.eval()
        self.vnf_placement_model.load_state_dict(torch.load(f'{DEFAULT_PARAMETER_PATH_PREFIX}{self.edge_name}-{self.info.max_vnf_num}_vnf_placement_model.pth'))
        self.vnf_placement_model.eval()

    def set_train(self) -> None:
        self.vnf_selection_model.train()
        self.vnf_placement_model.train()
    
    def set_eval(self) -> None:
        self.vnf_selection_model.eval()
        self.vnf_placement_model.eval()

@dataclass
class TrainArgs:
    srv_n: int
    sfc_n: int
    max_vnf_num: int
    seed: int
    max_episode_num: int
    debug_every_n_episode: int
    evaluate_every_n_episode: int


def train(agent: DQNAgent, make_env_fn: Callable, args: TrainArgs, file_name_prefix: str):
    agent.set_train()
    env = make_env_fn(args.seed)
    training_start = time.time()

    ch_slp_srv = []
    init_slp_srv = []
    final_slp_srv = []
    ch_sfc_in_same_srv = []
    init_sfc_in_same_srv = []
    final_sfc_in_same_srv = []
    steps = []
    rewards = []
    explorations = []
    debug_infos = []

    for episode in range(1, args.max_episode_num + 1):
        history = []
        state = env.reset()
        init_value = {
            "zero_util_cnt": get_zero_util_cnt(state),
            "sfc_cnt_in_same_srv": get_sfc_cnt_in_same_srv(state),
        }
        max_episode_len = env.max_episode_steps
        epsilon_sub = (episode / args.max_episode_num) * 0.5
        for step in range(1, max_episode_len + 1):
            action = agent.decide_action(state, epsilon_sub)
            history.append((state, action))
            next_state, reward, done = env.step(action)
            agent.update(state, action, reward, next_state)
            state = next_state
            if done:
                break
        rewards.append(reward)
        steps.append(step)
        explorations.append(agent.get_exploration_rate(epsilon_sub))
        final_value = {
            "zero_util_cnt": get_zero_util_cnt(state),
            "sfc_cnt_in_same_srv": get_sfc_cnt_in_same_srv(state),
        }
        ch_slp_srv.append(
            final_value["zero_util_cnt"] - init_value["zero_util_cnt"])
        init_slp_srv.append(init_value["zero_util_cnt"])
        final_slp_srv.append(final_value["zero_util_cnt"])
        ch_sfc_in_same_srv.append(
            final_value["sfc_cnt_in_same_srv"] - init_value["sfc_cnt_in_same_srv"])
        init_sfc_in_same_srv.append(init_value["sfc_cnt_in_same_srv"])
        final_sfc_in_same_srv.append(final_value["sfc_cnt_in_same_srv"])

        debug_info = DebugInfo(
            timestamp=time.strftime("%H:%M:%S", time.gmtime(
                time.time() - training_start)),
            episode=episode,
            mean_100_step=np.mean(steps[-100:]),
            std_100_step=np.std(steps[-100:]),
            mean_100_init_slp_srv=np.mean(init_slp_srv[-100:]),
            std_100_init_slp_srv=np.std(init_slp_srv[-100:]),
            mean_100_final_slp_srv=np.mean(final_slp_srv[-100:]),
            std_100_final_slp_srv=np.std(final_slp_srv[-100:]),
            mean_100_change_slp_srv=np.mean(ch_slp_srv[-100:]),
            std_100_change_slp_srv=np.std(ch_slp_srv[-100:]),
            srv_n=env.api.srv_n,
            mean_100_init_sfc_in_same_srv=np.mean(init_sfc_in_same_srv[-100:]),
            std_100_init_sfc_in_same_srv=np.std(init_sfc_in_same_srv[-100:]),
            mean_100_final_sfc_in_same_srv=np.mean(
                final_sfc_in_same_srv[-100:]),
            std_100_final_sfc_in_same_srv=np.std(final_sfc_in_same_srv[-100:]),
            mean_100_change_sfc_in_same_srv=np.mean(ch_sfc_in_same_srv[-100:]),
            std_100_change_sfc_in_same_srv=np.std(ch_sfc_in_same_srv[-100:]),
            sfc_n=env.api.sfc_n,
            mean_100_exploration=np.mean(explorations[-100:]),
            std_100_exploration=np.std(explorations[-100:]),
            mean_100_reward=np.mean(rewards[-100:]),
            std_100_reward=np.std(rewards[-100:]),
        )
        debug_infos.append(debug_info)
        print_debug_info(debug_info, refresh=False)
        history.append((state, None))
        if episode % args.debug_every_n_episode == 0:
            print_debug_info(debug_info, refresh=True)
        if episode % args.evaluate_every_n_episode == 0:
            evaluate(agent, make_env_fn, seed=args.seed,
                     file_name=f'{file_name_prefix}_episode{episode}')
            agent.set_train()
        if env._get_consolidation() and env._get_consolidation().active_flag == False :
            print("Early stop from swagger")
            break

    pd.DataFrame(debug_infos).to_csv(
        f'{file_name_prefix}_debug_info.csv', index=False)


def evaluate(agent: DQNAgent, make_env_fn: Callable, seed: int = 927, file_name: str = 'test'):
    agent.set_eval()
    env = make_env_fn(seed)
    # draw graph
    state = env.reset()
    srv_n = env.api.srv_n
    sfc_n = env.api.sfc_n
    max_vnf_num = env.api.max_vnf_num
    srv_cpu_cap = env.api.srv_cpu_cap
    srv_mem_cap = env.api.srv_mem_cap
    history = []
    while True:
        action = agent.decide_action(state, 0)
        history.append((state, action))
        state, reward, done = env.step(action)
        agent.memory.buffer[-1].reward = reward
        if done:
            break
    history.append((state, None))
    
    save_animation(
        srv_n=srv_n, sfc_n=sfc_n, vnf_n=max_vnf_num,
        srv_mem_cap=srv_mem_cap, srv_cpu_cap=srv_cpu_cap,
        history=history, path=f'{file_name}.mp4',
    )

def extract_best_policy(agent: DQNAgent, make_env_fn: Callable, seed: int = 927):
    agent.set_eval()
    env = make_env_fn(seed)
    
    state = env.reset()
    policy = []
    max_r = -1
    max_idx = 0
    for idx in range(env.max_episode_steps * 2):
        action = agent.decide_action(state, 0)
        next_state, _, done = env.step(action)
        r = env._calc_reward(True, state, next_state, True)
        if max_r < r:
            max_r = r
            max_idx = idx
        state = next_state
        policy.append(action)
        if done:
            break
    policy = policy[:max_idx+1]
    return policy

def run_policy(make_env_fn: Callable, policy: List[Action], seed: int = 927, is_trained: bool = False):
    env = make_env_fn(seed)
    state = env.reset()
    # for drawing graph
    srv_n = env.api.srv_n
    sfc_n = env.api.sfc_n
    max_vnf_num = env.api.max_vnf_num
    srv_cpu_cap = env.api.srv_cpu_cap
    srv_mem_cap = env.api.srv_mem_cap
    history = []
    for action in policy:
        history.append((state, action))
        state, _, done = env.step(action)
        if done:
            break
    history.append((state, None))

    # Get the current time
    current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    if not is_trained:
        save_animation(srv_n, sfc_n, max_vnf_num, srv_mem_cap,
                   srv_cpu_cap, history, f'{DEFAULT_RESULT_PATH_PREFIX}{env._get_consolidation().name}_{max_vnf_num}_final_{current_time}.mp4')


def start(consolidation, vnf_num=0):
    seed = 927  
    

    is_trained= consolidation.is_trained
    #consolidation.name
    #consolidation.flag


    def make_testbed_env_fn(seed): return Environment(
        api=Testbed(consolidation),
        seed=seed,
    )

    #Openstack Args
    env_info = make_testbed_env_fn(seed)

    srv_n = len(env_info._get_srvs())
    sfc_n = len(env_info._get_sfcs())
    max_vnf_num = len(env_info._get_vnfs()) if vnf_num == 0 else vnf_num
    srv_cpu_cap = env_info._get_edge().cpu_cap
    srv_mem_cap = env_info._get_edge().mem_cap

    if max_vnf_num == 0: return
        
    device = get_device()
    agent_info = DQNAgentInfo(
        edge_name = consolidation.name,
        srv_n=srv_n,
        sfc_n=sfc_n,
        max_vnf_num=max_vnf_num,
        init_epsilon=0.5,
        final_epsilon=0.0,
        vnf_s_lr=1e-3,
        vnf_p_lr=1e-3,
        gamma=0.99,
        vnf_s_model_info=DQNValueInfo(
            in_dim=VNF_SELECTION_IN_DIM_WITHOUT_SFC_NUM + MAXIMUM_SFC_NUM,
            hidden_dim=32,
            num_heads=4,
            num_blocks=4,
            device=device,
        ),
        vnf_p_model_info=DQNValueInfo(
            in_dim=VNF_PLACEMENT_IN_DIM_WITHOUT_SFC_NUM + MAXIMUM_SFC_NUM,
            hidden_dim=32,
            num_heads=4,
            num_blocks=4,
            device=device,
        ),
    )
    agent = DQNAgent(agent_info)

    train_args = TrainArgs(
        srv_n=srv_n,
        sfc_n=sfc_n,
        max_vnf_num=max_vnf_num,
        seed=seed,
        max_episode_num=10_000,
        debug_every_n_episode=500,
        evaluate_every_n_episode=500
    )

    if is_trained:
        try:
            agent.load()
        except:
            print('Runnable model is not exist.')
            print('Start learning for this system.')
            is_trained = False
    if not is_trained:
        def make_train_env_fn(seed): return Environment(
            api=Simulator(srv_n=srv_n, sfc_n=sfc_n, max_vnf_num=max_vnf_num,
                          srv_cpu_cap=srv_cpu_cap, srv_mem_cap=srv_mem_cap, vnf_types=[(1, 0.5), (1, 1), (2, 1), (2, 2), (4, 2), (4, 4), (8, 4), (8, 8)],
                          srvs=Testbed(consolidation).srvs),
            seed=seed,
        )
        train(agent, make_train_env_fn, train_args,
              file_name_prefix=f'{DEFAULT_RESULT_PATH_PREFIX}testebed-{agent_info.edge_name}-{max_vnf_num}')
        agent.save()
    def make_policy_extractor_env_fn(seed): return Environment(
        api=TestbedSimulator(consolidation),
        seed=seed,
    )

    # extract optimal policy
    # in this system we don't know what is the best step size.
    # so, we first simulate testbed environment and extract action list(policy).
    policy = extract_best_policy(agent, make_policy_extractor_env_fn, seed) 

    # run upper policy in testbed
    run_policy(make_testbed_env_fn, policy, is_trained)
