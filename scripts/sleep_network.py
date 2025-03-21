import os
import torch
import re
import glob
import pdb
import pickle
import itertools
import torch.nn as nn
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from pathlib import Path
from copy import deepcopy
from datetime import date
from scipy.stats import special_ortho_group
from scipy.ndimage import gaussian_filter1d

plt.style.use('ggplot')


class ConsolidationNetwork(nn.Module):
    def __init__(self, nneurons: int = 100, nganglia: int = 20, save_path: str = None, lr: float =1e-3,
                 device: torch.device = None, ncontexts: int = 2, dt: float=5e-2, tau: float = .15):
        super().__init__()

        if device is None:
            self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
            device = self.device
        else:
            self.device = device

        J_mat = (1.2 * np.random.randn(nneurons, nneurons)/np.sqrt(nneurons))
        self.bg_rank = nganglia
        self.J = nn.Parameter(torch.from_numpy(J_mat.astype(np.float32)).to(device))
        self.U = torch.from_numpy((np.random.randn(nneurons, nganglia) / np.sqrt(nneurons)).astype(np.float32)
                                   ).to(device)
        self.V = torch.from_numpy((np.random.randn(nganglia, nneurons) / np.sqrt(nneurons)).astype(np.float32)
                                   ).to(device)
        self.B_m1 = nn.Parameter(torch.from_numpy(np.random.randn(nneurons, 1).astype(np.float32)).to(device))
        self.B_bg = nn.Parameter(torch.from_numpy(np.zeros(nganglia).astype(np.float32)).to(device))
        self.Wout = nn.Parameter(torch.from_numpy((np.random.randn(1, nneurons)/np.sqrt(nneurons)).astype(np.float32)).to(device))
        self.I_go = nn.Parameter(torch.from_numpy(np.random.randn(nneurons, 1).astype(np.float32)).to(device))
        # self.I_fix = nn.Parameter(torch.from_numpy(np.random.randn(nneurons, 1).astype(np.float32)).to(device))
        # self.I_speed = nn.Parameter(torch.from_numpy(np.random.randn(nneurons, 1).astype(np.float32)).to(device))
        self.neural_nonlinearity = nn.Softplus()
        self.dt = dt
        self.tau = tau
        self.opt = torch.optim.Adam(self.parameters(), lr=lr)
        self.save_path = save_path
        self.replay_buffer = None
        self.Pulses = None
        self.fixation = None
        self.pulse_times = None
        self.total_time = None
        self.Loss = None
        self.g1, self.g2 = None, None
        self.kappaog = None
        self.new_targ_params = None
        self.create_gos_and_targets()
        # self.create_targets()

    def create_gos(self, total_time: int=500, n_unique_pulses: int = 10):
        device = self.device
        pulse_times = np.linspace(total_time * 1 / 10, total_time * 4 / 10, n_unique_pulses).astype(int)

        pulses = np.zeros((total_time, n_unique_pulses))
        fixation = np.ones((total_time, n_unique_pulses))

        for idx, pulse_start in enumerate(pulse_times):
            pulses[pulse_start: pulse_start + 10, idx] = 1
            fixation[pulse_start:, idx] = 0
        pulses = gaussian_filter1d(pulses, sigma=1, axis=0)
        pulses = torch.from_numpy(pulses.astype(np.float32)).to(device)
        fixation = gaussian_filter1d(fixation, sigma=1, axis=0)
        fixation = torch.from_numpy(fixation.astype(np.float32)).to(device)
        self.Pulses = pulses
        self.fixation = fixation
        self.pulse_times = pulse_times
        self.total_time = total_time
        self.Loss = []
        return pulses

    def create_targets(self, frequency: float = 1, delay: int = 0,
                       amplitude: int = 1, amplitude_modulated: bool = False,
                       **kwargs):
        pulse_times = self.pulse_times
        targets = np.zeros((self.total_time, pulse_times.shape[0]))
        times = np.arange(self.total_time)
        period = int(2 * np.pi / (self.dt))
        if amplitude_modulated:
            for idx, pulse in enumerate(pulse_times):
                amplitudes = amplitude * np.sin((times - pulse - delay) * (frequency / 2) * self.dt)
                targets[:, idx] = amplitudes * np.sin((times - pulse - delay) * frequency * self.dt)
                targets[:(pulse + delay), idx] = 0
                targets[(pulse + delay + 3 * period):, idx] = 0
        else:
            for idx, pulse in enumerate(pulse_times):
                targets[:, idx] = amplitude * np.sin((times - pulse - delay) * frequency * self.dt)
                targets[:(pulse + delay), idx] = 0
                targets[(pulse + delay + 3 * period):, idx] = 0
        self.Targets = targets

    def create_gos_and_targets(self, total_time: int=500, n_unique_pulses: int = 10, pulse_width: int = 10,
                               frequency: float = 1, delay: int = 0, amplitude: int = 1):
        device = self.device
        pulse_times = np.linspace(total_time * 1 / 10, total_time * 4 / 10, n_unique_pulses).astype(int)

        pulses = np.zeros((total_time, n_unique_pulses))
        fixation = np.ones((total_time, n_unique_pulses))
        targets = np.zeros((total_time, n_unique_pulses))
        times = np.arange(total_time)
        period = int(2 * np.pi / (self.dt))

        for idx, pulse_start in enumerate(pulse_times):
            pulses[pulse_start: pulse_start + pulse_width, idx] = 1
            fixation[pulse_start:, idx] = 0
            targets[:, idx] = amplitude * np.sin((times - pulse_start - delay) * frequency * self.dt)
            targets[:(pulse_start + delay), idx] = 0
            targets[(pulse_start + delay + 3 * period):, idx] = 0
            pulses[(pulse_start + 3 * period): (pulse_start + pulse_width + 3 * period), idx] = -1


        pulses = gaussian_filter1d(pulses, sigma=1, axis=0)
        pulses = torch.from_numpy(pulses.astype(np.float32)).to(device)
        fixation = gaussian_filter1d(fixation, sigma=1, axis=0)
        fixation = torch.from_numpy(fixation.astype(np.float32)).to(device)
        self.Pulses = pulses
        self.fixation = fixation
        self.pulse_times = pulse_times
        self.total_time = total_time
        self.Loss = []
        self.Targets = targets
        return pulses

    def forward(self, targets: np.ndarray = None, triggers: np.ndarray = None,
                include_bg: bool = True, speed: int=1,
                noise_scale: float = 0.15):
        assert targets.shape[1] == triggers.shape[0], 'The number of targets must be equal to the number of go cues.'
        device = self.device
        batch_size = targets.shape[1]
        Targets = torch.from_numpy(targets.astype(np.float32))
        go_cues = self.Pulses[:, triggers]
        position_store = torch.zeros(self.total_time, batch_size)
        xm1 = torch.randn((self.J.shape[0], batch_size), device=device) / np.sqrt(self.J.shape[0])
        rm1 = self.neural_nonlinearity(xm1)
        if include_bg:
            rthal = torch.diag((self.B_bg))
        else:
            rthal = torch.diag(torch.zeros(self.U.shape[1], device=device))

        for ti in range(self.total_time):
            xm1 = xm1 + self.dt/self.tau * (-xm1 + (self.J + self.U @ rthal @ self.V) @ rm1 + self.B_m1 +
                                   self.I_go * go_cues[ti] +
                                            torch.randn(rm1.shape, device=device) * np.sqrt(2 * noise_scale ** 2 *
                                                                             (self.tau / self.dt)))
            rm1 = self.neural_nonlinearity(xm1)
            position_store[ti] = self.Wout @ rm1
        loss = ((Targets - position_store)**2).mean(axis=0).mean()
        plt.clf()
        plt.plot(targets[:, 0], label='Target')
        plt.plot(go_cues[:, 0].cpu(), label='Go Cue')
        plt.plot(position_store[:, 0].detach().cpu(), label='Actual')
        plt.legend()
        plt.pause(0.01)
        return loss

    def plot_different_gains(self, trigger: int = 0, save_path: str = None):
        device = self.device
        target = self.Targets[:, trigger]
        gains = np.linspace(-1, 1)
        position_store = torch.zeros(self.total_time, gains.shape[0])

        go_cues = self.Pulses[:, trigger].unsqueeze(1)
        N = gains.shape[0]
        og_cycler = plt.rcParams["axes.prop_cycle"]
        plt.rcParams["axes.prop_cycle"] = plt.cycler("color", plt.cm.viridis(np.linspace(0, 1, N)))
        plt.figure()
        plt.plot(target, label='Target', color='black', ls='--')
        plt.plot(go_cues.cpu(), label='Go Cue', color='red')
        for idx, gain in enumerate(gains):
            rthal = torch.diag(gain * (self.B_bg))
            xm1 = torch.randn((self.J.shape[0], gains.shape[0]), device=device) / np.sqrt(self.J.shape[0])
            rm1 = self.neural_nonlinearity(xm1)
            for ti in range(self.total_time):
                xm1 = xm1 + self.dt / self.tau * (-xm1 + (self.J + self.U @ rthal @ self.V)
                                                                  @ rm1 + self.B_m1 +
                                                  self.I_go * go_cues[ti])
                rm1 = self.neural_nonlinearity(xm1)
                position_store[ti, idx] = (self.Wout @ rm1)[:, 0]

            plt.plot(position_store[:, idx].detach().cpu(), alpha=.5)
        plt.legend()
        f_path = save_path + '/output_gain_interpolation'
        plt.savefig(f_path)
        plt.pause(1)
        plt.rcParams["axes.prop_cycle"] = og_cycler

    def plot_variances(self, trigger: int = 0, samples: int = 100):
        target = self.Targets[:, trigger]
        device = self.device
        position_store = torch.zeros(self.total_time, samples)
        magnitude = torch.linalg.norm(self.B_bg)
        go_cues = self.Pulses[:, trigger].unsqueeze(1)
        plt.figure()
        plt.plot(target, label='Target', color='black', ls='--')
        plt.plot(go_cues.cpu(), label='Go Cue', color='red')

        for idx in range(samples):
            rthal = torch.diag( torch.randn(self.B_bg.shape, device=device) * magnitude + self.B_bg)
            xm1 = torch.randn((self.J.shape[0], samples), device=device) / np.sqrt(self.J.shape[0])
            rm1 = self.neural_nonlinearity(xm1)
            for ti in range(self.total_time):
                xm1 = xm1 + self.dt / self.tau * (-xm1 + (self.J + self.U @ rthal @ self.V)
                                                                  @ rm1 + self.B_m1 +
                                                  self.I_go * go_cues[ti])
                rm1 = self.neural_nonlinearity(xm1)
                position_store[ti, idx] = (self.Wout @ rm1)[:, 0]

            plt.plot(position_store[:, idx].detach().cpu(), alpha=.5)
        plt.legend()
        plt.pause(1)

        vars = position_store.var(axis=1).detach().cpu().numpy()
        plt.figure()
        plt.plot(vars)
        plt.xlabel('Time'
        )
        plt.ylabel('Variance')
        plt.pause(1)
        pdb.set_trace()

    def fit_initial_m1(self, batch_size: int = 50, trials: int = 1000):
        m1_params = [self.J, self.Wout, self.I_go, self.B_m1]
        optimizer = torch.optim.Adam(m1_params, lr=1e-3)
        # plt.figure()
        for trial in range(trials):
            optimizer.zero_grad()
            triggers = np.random.randint(0, self.Targets.shape[1], batch_size)
            targets = self.Targets[:, triggers]
            loss = self.forward(targets, triggers, include_bg=False)
            self.Loss.append(loss.item())
            loss.backward()
            optimizer.step()
            # plt.clf()
            # plt.scatter(range(len(self.Loss)), self.Loss)
            #
            # plt.pause(1)
        for param in m1_params:
            param.requires_grad = False

    def fit_bg_loop(self, batch_size: int = 100, trials: int = 2500, g1: float = 0,
                    g2: float = 0, make_plots: bool = True, bg_rank: int = None,
                    frequency: float = 1, amplitude: float = 1, amp_modulated: bool = False,
                    save_path: str = None,
                    **kwargs):
        if not bg_rank is None:
            self.bg_rank = bg_rank
        self.new_targ_params = {'frequency': frequency, 'amplitude': amplitude, 'amplitude_mod': amp_modulated}
        self.set_thalamic_weights(g1=g1, g2=g2)
        self.create_targets(frequency=frequency, amplitude=amplitude, amplitude_modulated=amp_modulated)
        p = np.polyfit(np.arange(len(self.Loss)), np.log(self.Loss), 1)
        self.kappaog = p[0]
        bg_params = [self.B_bg]
        optimizer = torch.optim.Adam(bg_params, lr=1e-3, weight_decay=1e-3)
        plt.figure()
        # plt.figure()
        self.Loss = []
        with torch.no_grad():
            triggers = np.random.randint(0, self.Targets.shape[1], batch_size)
            targets = self.Targets[:, triggers]
            self.forward(targets, triggers, include_bg=False)
        plt.pause(1)
        plt.figure()
        for trial in range(trials):
            optimizer.zero_grad()
            triggers = np.random.randint(0, self.Targets.shape[1], batch_size)
            targets = self.Targets[:, triggers]
            loss = self.forward(targets, triggers, include_bg=True)
            self.Loss.append(loss.item())
            loss.backward()
            optimizer.step()
            # plt.clf()
            # plt.scatter(range(len(self.Loss)), self.Loss)
            #
            # plt.pause(1)
        self.save_bg_results()
        if make_plots:
            j0 = self.J.detach().cpu().numpy()
            plt.pause(1)
            plt.figure()
            for g in np.linspace(-1, 1):
                j_lr = (self.U.detach().cpu().numpy() @ np.diag(g * self.B_bg.detach().cpu().numpy())
                        @ self.V.detach().cpu().numpy())
                evals_lr = np.linalg.eigvals(j0 + j_lr)
                plt.scatter(np.real(evals_lr), np.imag(evals_lr), c=g*np.ones(j_lr.shape[0]), label='Perturbed',
                            vmin=-1, vmax=1)
            plt.colorbar(label='gain')
            fig_path = save_path + '/eigenspectrum_interpolation'
            plt.savefig(fig_path)
            plt.pause(1)
            plt.figure()
            with torch.no_grad():
                triggers = np.random.randint(0, self.Targets.shape[1], batch_size)
                targets = self.Targets[:, triggers]
                self.forward(targets, triggers, include_bg=False)

            self.plot_different_gains(save_path=save_path)
            # self.plot_variances()

    def set_thalamic_weights(self, g1: float = 0, g2: float = 0):
        J = self.J.detach().cpu().numpy()
        self.g1, self.g2 = g1, g2
        U, S, Vh = np.linalg.svd(J)
        device = self.device
        self.U = (torch.from_numpy(((np.sqrt(1 - g1**2)) * U[:, :self.bg_rank]
                                   + g1**2 * np.random.randn(J.shape[0], self.bg_rank)).astype(np.float32)
                                   ).to(device))
        self.V = (torch.from_numpy(((np.sqrt(1 - g2**2)) * Vh[:self.bg_rank]
                                   + g2 ** 2 * np.random.randn(self.bg_rank, J.shape[0])).astype(np.float32)
                                   ).to(device))
        self.B_bg = nn.Parameter(torch.from_numpy(np.zeros(self.bg_rank).astype(np.float32)).to(device))
        return None

    def save_model(self, save_path: str = None):
        state_dict = self.state_dict()
        data_dict = {'model_state': state_dict, 'full_model': self}

        with open(save_path, 'wb') as handle:
            pickle.dump(data_dict, handle, protocol=pickle.HIGHEST_PROTOCOL)

    def save_bg_results(self):
        cwd = os.getcwd()
        cwd_path = Path(cwd)
        save_path = cwd_path / 'data' / 'bg_data.csv'
        p = np.polyfit(np.arange(len(self.Loss)), np.log(self.Loss), 1)
        kappa = p[0]
        curr_dict = {'br_rank': [self.bg_rank], 'g1': [self.g1],
                                 'g2': [self.g2], 'Loss': [self.Loss[-1]],
                                     'Effective_lr': [-kappa],
                                     'Effective_lr0': [-self.kappaog]}
        data_dict = {**curr_dict, **self.new_targ_params}
        if save_path.exists():
            data = pd.read_csv(save_path)
            new_data = pd.DataFrame(data_dict)
            data = pd.concat([data, new_data], ignore_index=True)
        else:
            data = pd.DataFrame(data_dict)
        data.reset_index()
        data.to_csv(save_path, index=None)


if __name__ == '__main__':
    cwd = os.getcwd()
    cwd_path = Path(cwd)
    save_folder = cwd_path / 'data'
    save_folder.mkdir(exist_ok=True)
    save_path = save_folder / 'base_model.pkl'
    with open(save_path, 'rb') as handle:
        data = pickle.load(handle)
    model = data['full_model']

    g1s = np.linspace(0, 1, 6)
    g2s = np.linspace(0, 1, 6)
    trial_types = ['frequency', 'amplitude', 'freqency_and_amplitude', 'amplitude_modulated']
    param_values = [{'frequency': 2, 'trials': 500}, {'amplitude': 2.5, 'trials': 500},
                    {'frequency': 2, 'amplitude': 2.5, 'trials': 2500},
                    {'amp_modulated': True, 'trials': 5000}]

    date_str = date.today().strftime("%Y-%m-%d")
    date_save_path = cwd_path / 'results' / date_str
    date_save_path.mkdir(exist_ok=True)
    reg_exp = 'trial_(\d)'
    for bg_rank in [25, 50, 75, 100]:
        for g1, g2 in itertools.product(g1s, g2s):
            param_string = f'r_{bg_rank}_g1_{g1}_g2_{g2}'
            param_fpath = date_save_path / param_string
            param_fpath.mkdir(exist_ok=True)
            for trial_type, params in zip(trial_types, param_values):
                save_fpath = param_fpath / f'{trial_type}'
                save_fpath.mkdir(exist_ok=True)
                # all_files = glob.glob(save_fpath + '/*')
                all_subfolders = list(save_fpath.iterdir())
                included_files = [file.stem for file in all_subfolders if re.search(reg_exp, file.stem)]
                trial_save_path = save_fpath / f'trial_{len(included_files)}'
                trial_save_path.mkdir(exist_ok=True)
                str_path = trial_save_path.absolute().as_posix()
                model = deepcopy(data['full_model'])
                device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
                model.to(device)
                model.fit_bg_loop(g1=g1, g2=g2, bg_rank=bg_rank, save_path=str_path,
                                  **params)

                plt.close('all')

    test = ConsolidationNetwork()
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    test.to(device)
    test.fit_initial_m1()
    #
    # pdb.set_trace()
    # print('Begining BG')
    # test.fit_bg_loop()