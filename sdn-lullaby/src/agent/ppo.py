import os
import time
from typing import Callable, List
from datetime import datetime

import torch
import pandas as pd
from dataclasses import dataclass

from src.dataType import State, Action
from src.api.simulator import Simulator
from src.api.testbed import Testbed
from src.api.testbed_simulator import TestbedSimulator
from src.memory.episode import EpisodeMemory
from src.env import Environment, MultiprocessEnvironment
from src.const import VNF_SELECTION_IN_DIM_WITHOUT_SFC_NUM, VNF_PLACEMENT_IN_DIM_WITHOUT_SFC_NUM, MAXIMUM_SFC_NUM
from src.model.ppo import PPOPolicyInfo, PPOValueInfo, PPOPolicy, PPOValue
from src.utils import (
    get_device,
    logit_to_prob,
    save_animation,
    print_debug_info,
    get_info_from_logits,
    get_possible_actions,
    convert_state_to_vnf_selection_input,
    convert_state_to_vnf_placement_input,
)

torch.backends.cudnn.enabled = False
torch.cuda.is_available = lambda: False

DEFAULT_RESULT_PATH_PREFIX = 'result/ppo/'
DEFAULT_PARAMETER_PATH_PREFIX = 'param/ppo/'

os.makedirs(DEFAULT_RESULT_PATH_PREFIX, exist_ok=True)
os.makedirs(DEFAULT_PARAMETER_PATH_PREFIX, exist_ok=True)


@dataclass
class PPOAgentInfo:
    srv_n: int
    sfc_n: int
    max_vnf_num: int
    vnf_s_policy_info: PPOPolicyInfo
    vnf_p_policy_info: PPOPolicyInfo
    vnf_s_value_info: PPOValueInfo
    vnf_p_value_info: PPOValueInfo
    vnf_s_policy_lr: float
    vnf_p_policy_lr: float
    vnf_s_value_lr: float
    vnf_p_value_lr: float
    vnf_s_policy_clip_range: float
    vnf_p_policy_clip_range: float
    vnf_s_entropy_loss_weight: float
    vnf_p_entropy_loss_weight: float
    vnf_s_policy_max_grad_norm: float
    vnf_p_policy_max_grad_norm: float
    vnf_s_value_clip_range: float
    vnf_p_value_clip_range: float
    vnf_s_value_max_grad_norm: float
    vnf_p_value_max_grad_norm: float
    edge_name: str


class PPOAgent:
    def __init__(self, info: PPOAgentInfo):
        self.info = info

        self.vnf_s_policy = PPOPolicy(info.vnf_s_policy_info)
        self.vnf_p_policy = PPOPolicy(info.vnf_p_policy_info)
        self.vnf_s_value = PPOValue(info.vnf_s_value_info)
        self.vnf_p_value = PPOValue(info.vnf_p_value_info)

        self.vnf_s_policy_optimizer = torch.optim.Adam(
            self.vnf_s_policy.parameters(), lr=info.vnf_s_policy_lr)
        self.vnf_p_policy_optimizer = torch.optim.Adam(
            self.vnf_p_policy.parameters(), lr=info.vnf_p_policy_lr)
        self.vnf_s_value_optimizer = torch.optim.Adam(
            self.vnf_s_value.parameters(), lr=info.vnf_s_value_lr)
        self.vnf_p_value_optimizer = torch.optim.Adam(
            self.vnf_p_value.parameters(), lr=info.vnf_p_value_lr)
        self.edge_name = info.edge_name

    def decide_action(self, state: State, greedy: bool = False) -> Action:
        p_actions = get_possible_actions(
            state, self.info.vnf_s_policy_info.out_dim)
        if greedy:  # Greedy
            vnf_s_in = convert_state_to_vnf_selection_input(
                state, self.info.vnf_s_policy_info.out_dim)
            vnf_s_out = self.vnf_s_policy(vnf_s_in).detach().cpu()
            vnf_s_out = vnf_s_out * torch.tensor([True if len(
                p_actions[vnf_id]) > 0 else False for vnf_id in range(self.info.vnf_s_policy_info.out_dim)])
            vnf_id = int(vnf_s_out.max())
            vnf_p_in = convert_state_to_vnf_placement_input(state, vnf_id)
            vnf_p_out = self.vnf_p_policy(vnf_p_in).detach().cpu()
            vnf_p_out = vnf_p_out * torch.tensor([True if srv_id in p_actions[vnf_id]
                                                 else False for srv_id in range(self.info.vnf_p_policy_info.out_dim)])
            srv_id = int(vnf_p_out.max())
        else:  # Stochastic
            vnf_s_in = convert_state_to_vnf_selection_input(
                state, self.info.vnf_s_policy_info.out_dim)
            vnf_s_out = self.vnf_s_policy(vnf_s_in).detach().cpu()
            vnf_s_out = vnf_s_out * torch.tensor([True if len(
                p_actions[vnf_id]) > 0 else False for vnf_id in range(self.info.vnf_s_policy_info.out_dim)])
            vnf_id, _, _ = get_info_from_logits(vnf_s_out.unsqueeze(0))
            vnf_id = int(vnf_id)
            vnf_p_in = convert_state_to_vnf_placement_input(state, vnf_id)
            vnf_p_out = self.vnf_p_policy(vnf_p_in).detach().cpu()
            vnf_p_out = vnf_p_out * torch.tensor([True if srv_id in p_actions[vnf_id]
                                                 else False for srv_id in range(self.info.vnf_p_policy_info.out_dim)])
            srv_id, _, _ = get_info_from_logits(vnf_p_out.unsqueeze(0))
            srv_id = int(srv_id)
        return Action(vnf_id=vnf_id, srv_id=srv_id)

    def update_policy(self, vnf_s_ins: torch.Tensor, vnf_p_ins: torch.Tensor, actions: torch.Tensor, returns: torch.Tensor, gaes: torch.Tensor, logpas: torch.Tensor, values: torch.Tensor) -> None:
        # TODO: Possible Action 고려안했는데 생각해볼만하다.
        actions = actions.to(self.info.vnf_s_policy_info.device)
        logpas = logpas.to(self.info.vnf_s_policy_info.device)
        gaes = gaes.to(self.info.vnf_s_policy_info.device)

        vnf_s_outs = self.vnf_s_policy(vnf_s_ins)
        vnf_s_probs = logit_to_prob(vnf_s_outs)
        vnf_s_dist = torch.distributions.Categorical(probs=vnf_s_probs)
        vnf_s_logpas_pred = vnf_s_dist.log_prob(actions[:, 0])
        vnf_s_entropies_pred = vnf_s_dist.entropy()

        vnf_s_rations = torch.exp(vnf_s_logpas_pred - logpas[:, 0])
        vnf_s_pi_obj = vnf_s_rations * gaes[:, 0]
        vnf_s_pi_obj_clipped = vnf_s_rations.clamp(
            1.0 - self.info.vnf_s_policy_clip_range, 1.0 + self.info.vnf_s_policy_clip_range) * gaes[:, 0]

        vnf_s_policy_loss = - \
            torch.min(vnf_s_pi_obj, vnf_s_pi_obj_clipped).mean()
        vnf_s_entropy_loss = -vnf_s_entropies_pred.mean()
        vnf_s_policy_loss = vnf_s_policy_loss + \
            self.info.vnf_s_entropy_loss_weight * vnf_s_entropy_loss

        self.vnf_s_policy_optimizer.zero_grad()
        vnf_s_policy_loss.backward()
        torch.nn.utils.clip_grad_norm_(
            self.vnf_s_policy.parameters(), self.info.vnf_s_policy_max_grad_norm)
        self.vnf_s_policy_optimizer.step()

        vnf_p_outs = self.vnf_p_policy(vnf_p_ins)
        vnf_p_probs = logit_to_prob(vnf_p_outs)
        vnf_p_dist = torch.distributions.Categorical(probs=vnf_p_probs)
        vnf_p_logpas_pred = vnf_p_dist.log_prob(actions[:, 1])
        vnf_p_entropies_pred = vnf_p_dist.entropy()

        vnf_p_rations = torch.exp(vnf_p_logpas_pred - logpas[:, 1])
        vnf_p_pi_obj = vnf_p_rations * gaes[:, 1]
        vnf_p_pi_obj_clipped = vnf_p_rations.clamp(
            1.0 - self.info.vnf_p_policy_clip_range, 1.0 + self.info.vnf_p_policy_clip_range) * gaes[:, 1]

        vnf_p_policy_loss = - \
            torch.min(vnf_p_pi_obj, vnf_p_pi_obj_clipped).mean()
        vnf_p_entropy_loss = -vnf_p_entropies_pred.mean()
        vnf_p_policy_loss = vnf_p_policy_loss + \
            self.info.vnf_p_entropy_loss_weight * vnf_p_entropy_loss

        self.vnf_p_policy_optimizer.zero_grad()
        vnf_p_policy_loss.backward()
        torch.nn.utils.clip_grad_norm_(
            self.vnf_p_policy.parameters(), self.info.vnf_p_policy_max_grad_norm)
        self.vnf_p_policy_optimizer.step()

    def update_value(self, vnf_s_ins: torch.Tensor, vnf_p_ins: torch.Tensor, actions: torch.Tensor, returns: torch.Tensor, gaes: torch.Tensor, logpas: torch.Tensor, values: torch.Tensor) -> None:
        vnf_s_returns = returns[:, 0].to(self.info.vnf_s_value_info.device)
        vnf_p_returns = returns[:, 1].to(self.info.vnf_p_value_info.device)
        vnf_s_values = values[:, 0].to(self.info.vnf_s_value_info.device)
        vnf_p_values = values[:, 1].to(self.info.vnf_p_value_info.device)

        vnf_s_value_pred = self.vnf_s_value(vnf_s_ins)
        vnf_s_value_pred_clipped = vnf_s_values + \
            (vnf_s_value_pred - vnf_s_values).clamp(-self.info.vnf_s_value_clip_range,
                                                    self.info.vnf_s_value_clip_range)

        vnf_s_value_loss = (vnf_s_value_pred - vnf_s_returns).pow(2)
        vnf_s_value_loss_clipped = (
            vnf_s_value_pred_clipped - vnf_s_returns).pow(2)
        vnf_s_value_loss = 0.5 * \
            torch.max(vnf_s_value_loss, vnf_s_value_loss_clipped).mean()

        self.vnf_s_value_optimizer.zero_grad()
        vnf_s_value_loss.backward()
        torch.nn.utils.clip_grad_norm_(
            self.vnf_s_value.parameters(), self.info.vnf_s_value_max_grad_norm)
        self.vnf_s_value_optimizer.step()

        vnf_p_value_pred = self.vnf_p_value(vnf_p_ins)
        vnf_p_value_pred_clipped = vnf_p_values + \
            (vnf_p_value_pred - vnf_p_values).clamp(-self.info.vnf_p_value_clip_range,
                                                    self.info.vnf_p_value_clip_range)

        vnf_p_value_loss = (vnf_p_value_pred - vnf_p_returns).pow(2)
        vnf_p_value_loss_clipped = (
            vnf_p_value_pred_clipped - vnf_p_returns).pow(2)
        vnf_p_value_loss = 0.5 * \
            torch.max(vnf_p_value_loss, vnf_p_value_loss_clipped).mean()

        self.vnf_p_value_optimizer.zero_grad()
        vnf_p_value_loss.backward()
        torch.nn.utils.clip_grad_norm_(
            self.vnf_p_value.parameters(), self.info.vnf_p_value_max_grad_norm)
        self.vnf_p_value_optimizer.step()

    def get_logpas_pred(self, vnf_s_ins: torch.Tensor, vnf_p_ins: torch.Tensor, actions: torch.Tensor) -> torch.Tensor:
        actions = actions.to(self.info.vnf_s_policy_info.device)
        with torch.no_grad():
            vnf_s_logits = self.vnf_s_policy(vnf_s_ins)
            vnf_s_probs = logit_to_prob(vnf_s_logits)
            vnf_s_dist = torch.distributions.Categorical(probs=vnf_s_probs)
            vnf_s_logpas_pred = vnf_s_dist.log_prob(actions[:, 0])

            vnf_p_logits = self.vnf_p_policy(vnf_p_ins)
            vnf_p_probs = logit_to_prob(vnf_p_logits)
            vnf_p_dist = torch.distributions.Categorical(probs=vnf_p_probs)
            vnf_p_logpas_pred = vnf_p_dist.log_prob(actions[:, 1])

        return torch.stack([vnf_s_logpas_pred, vnf_p_logpas_pred], dim=1)

    def save(self) -> None:
        torch.save(self.vnf_p_policy.state_dict(), f'{DEFAULT_PARAMETER_PATH_PREFIX}{self.edge_name}_{self.info.max_vnf_num}_vnf_p_policy.pt')
        torch.save(self.vnf_s_policy.state_dict(), f'{DEFAULT_PARAMETER_PATH_PREFIX}{self.edge_name}_{self.info.max_vnf_num}_vnf_s_policy.pt')

    def load(self) -> None:
        self.vnf_p_policy.load_state_dict(torch.load(f'{DEFAULT_PARAMETER_PATH_PREFIX}{self.edge_name}_{self.info.max_vnf_num}_vnf_p_policy.pt'))
        self.vnf_p_policy.eval()
        self.vnf_s_policy.load_state_dict(torch.load(f'{DEFAULT_PARAMETER_PATH_PREFIX}{self.edge_name}_{self.info.max_vnf_num}_vnf_s_policy.pt'))
        self.vnf_s_policy.eval()

    def set_train(self) -> None:
        self.vnf_p_policy.train()
        self.vnf_s_policy.train()

    def set_eval(self) -> None:
        self.vnf_p_policy.eval()
        self.vnf_s_policy.eval()


@dataclass
class TrainArgs:
    srv_n: int
    sfc_n: int
    max_vnf_num: int
    seed: int
    tau: float
    gamma: float
    n_workers: int
    batch_size: int
    update_epochs: int
    max_episode_num: int
    max_episode_steps: int
    memory_max_episode_num: int
    evaluate_every_n_episode: int
    policy_update_early_stop_threshold: float
    early_stop_patience: int


def train(agent: PPOAgent, make_env_fn: Callable, args: TrainArgs, file_name_prefix: str):
    agent.set_train()
    memory = EpisodeMemory(
        args.n_workers, args.batch_size,
        args.gamma, args.tau,
        args.memory_max_episode_num, args.max_episode_steps,
        args.srv_n, args.sfc_n, args.max_vnf_num,
        VNF_SELECTION_IN_DIM_WITHOUT_SFC_NUM + MAXIMUM_SFC_NUM, VNF_PLACEMENT_IN_DIM_WITHOUT_SFC_NUM + MAXIMUM_SFC_NUM,
    )

    env = make_env_fn(args.seed)

    mp_env = MultiprocessEnvironment(args.seed, args.n_workers, make_env_fn)
    debug_infos = []
    training_start = time.time()

    highest_reward = -float('inf')
    early_stop_cnt = 0
    for episode in range(args.memory_max_episode_num, args.max_episode_num+args.memory_max_episode_num, args.memory_max_episode_num):
        memory.fill(mp_env, agent)
        vnf_s_ins, vnf_p_ins, actions, _, _, logpas, _ = memory.samples(
            all=True)
        for _ in range(args.update_epochs):
            agent.update_policy(*memory.samples())
            logpas_pred = agent.get_logpas_pred(
                vnf_s_ins, vnf_p_ins, actions).cpu()
            early_stop = torch.abs(
                logpas - logpas_pred).mean() < args.policy_update_early_stop_threshold
            if early_stop:
                break
        for _ in range(args.update_epochs):
            agent.update_value(*memory.samples())
        debug_info = memory.get_debug_info(
            episode=episode, training_start=training_start)
        print_debug_info(debug_info, refresh=True)
        debug_infos.append(debug_info)
        memory.reset()
        if episode % args.evaluate_every_n_episode == 0:
            evaluate(agent, make_env_fn, args.seed,
                     f'{file_name_prefix}_episode{episode}')
            agent.set_train()
        if debug_info.mean_100_reward > highest_reward:
            highest_reward = debug_info.mean_100_reward
            early_stop_cnt = 0
            agent.save()
        early_stop_cnt += 1
        if early_stop_cnt > args.early_stop_patience:
            break
        if env._get_consolidation() and env._get_consolidation().active_flag == False:
            print("Early stop from swagger")
            break

    pd.DataFrame(debug_infos).to_csv(
        f'{file_name_prefix}_debug_info.csv', index=False)
    mp_env.close()


def evaluate(agent: PPOAgent, make_env_fn: Callable, seed: int = 927, file_name: str = 'test'):
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
    for _ in range(env.max_episode_steps):
        action = agent.decide_action(state)
        history.append((state, action))
        state, _, done = env.step(action)
        if done:
            break
    history.append((state, None))

    save_animation(srv_n, sfc_n, max_vnf_num, srv_mem_cap,
                   srv_cpu_cap, history, f'{file_name}.mp4')

def extract_best_policy(agent: PPOAgent, make_env_fn: Callable, seed: int = 927):
    agent.set_eval()
    env = make_env_fn(seed)
    
    state = env.reset()
    policy = []
    max_r = -1
    max_idx = 0
    for idx in range(env.max_episode_steps * 2):
        action = agent.decide_action(state)
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

    is_trained = consolidation.is_trained
    # consolidation.name
    # consolidation.flag

    def make_testbed_env_fn(seed): return Environment(
        api=Testbed(consolidation),
        seed=seed,
    )

    # Openstack Args
    env_info = make_testbed_env_fn(seed)

    srv_n = len(env_info._get_srvs())
    sfc_n = len(env_info._get_sfcs())
    max_vnf_num = len(env_info._get_vnfs()) if vnf_num == 0 else vnf_num
    srv_cpu_cap = env_info._get_edge().cpu_cap
    srv_mem_cap = env_info._get_edge().mem_cap

    if max_vnf_num == 0: return

    device = get_device()
    agent_info = PPOAgentInfo(
        srv_n=srv_n,
        sfc_n=sfc_n,
        max_vnf_num=max_vnf_num,
        edge_name=consolidation.name,
        vnf_s_policy_info=PPOPolicyInfo(
            in_dim=VNF_SELECTION_IN_DIM_WITHOUT_SFC_NUM + MAXIMUM_SFC_NUM,
            hidden_dim=32,
            out_dim=max_vnf_num,
            num_blocks=2,
            num_heads=2,
            device=device,
        ),
        vnf_p_policy_info=PPOPolicyInfo(
            in_dim=VNF_PLACEMENT_IN_DIM_WITHOUT_SFC_NUM + MAXIMUM_SFC_NUM,
            hidden_dim=32,
            out_dim=srv_n,
            num_blocks=2,
            num_heads=2,
            device=device,
        ),
        vnf_s_value_info=PPOValueInfo(
            in_dim=VNF_SELECTION_IN_DIM_WITHOUT_SFC_NUM + MAXIMUM_SFC_NUM,
            hidden_dim=32,
            seq_len=max_vnf_num,
            num_blocks=2,
            num_heads=2,
            device=device,
        ),
        vnf_p_value_info=PPOValueInfo(
            in_dim=VNF_PLACEMENT_IN_DIM_WITHOUT_SFC_NUM + MAXIMUM_SFC_NUM,
            hidden_dim=32,
            seq_len=srv_n,
            num_blocks=2,
            num_heads=2,
            device=device,
        ),
        vnf_s_policy_lr=1e-3,
        vnf_p_policy_lr=1e-3,
        vnf_s_value_lr=1e-3,
        vnf_p_value_lr=1e-3,
        vnf_s_policy_clip_range=0.1,
        vnf_p_policy_clip_range=0.1,
        vnf_s_entropy_loss_weight=0.01,
        vnf_p_entropy_loss_weight=0.01,
        vnf_s_policy_max_grad_norm=float('inf'),
        vnf_p_policy_max_grad_norm=float('inf'),
        vnf_s_value_clip_range=float('inf'),
        vnf_p_value_clip_range=float('inf'),
        vnf_s_value_max_grad_norm=float('inf'),
        vnf_p_value_max_grad_norm=float('inf'),
    )
    agent = PPOAgent(agent_info)

    train_args = TrainArgs(
        srv_n=srv_n,
        sfc_n=sfc_n,
        max_vnf_num=max_vnf_num,
        seed=seed,
        tau=0.97,
        gamma=0.99,
        n_workers=8,
        batch_size=32,
        update_epochs=80,
        max_episode_num=100_000,
        max_episode_steps=max_vnf_num,
        memory_max_episode_num=200,
        evaluate_every_n_episode=10_000,
        policy_update_early_stop_threshold=1e-3,
        early_stop_patience=50,
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
