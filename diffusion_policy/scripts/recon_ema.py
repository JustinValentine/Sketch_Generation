import click
from pathlib import Path
import numpy as np
import re
import hydra
from diffusion_policy.workspace.base_workspace import BaseWorkspace
import torch
import dill
import copy
import wandb
from bayes_opt import BayesianOptimization
from bayes_opt import UtilityFunction

def parse_std_list(s):
    if isinstance(s, list):
        return s

    # Parse raw values.
    raw = [None if v == '...' else float(v) for v in s.split(',')]

    # Fill in '...' tokens.
    out = []
    for i, v in enumerate(raw):
        if v is not None:
            out.append(v)
            continue
        if i - 2 < 0 or raw[i - 2] is None or raw[i - 1] is None:
            raise click.ClickException("'...' must be preceded by at least two floats")
        if i + 1 >= len(raw) or raw[i + 1] is None:
            raise click.ClickException("'...' must be followed by at least one float")
        if raw[i - 2] == raw[i - 1]:
            raise click.ClickException("The floats preceding '...' must not be equal")
        approx_num = (raw[i + 1] - raw[i - 1]) / (raw[i - 1] - raw[i - 2]) - 1
        num = round(approx_num)
        if num <= 0:
            raise click.ClickException("'...' must correspond to a non-empty interval")
        if abs(num - approx_num) > 1e-4:
            raise click.ClickException("'...' must correspond to an evenly spaced interval")
        for j in range(num):
            out.append(raw[i - 1] + (raw[i - 1] - raw[i - 2]) * (j + 1))

    # Validate.
    out = sorted(set(out))
    if not all(0.000 < v < 0.289 for v in out):
        raise click.ClickException('Relative standard deviation must be positive and less than 0.289')
    return out

def std_to_exp(std):
    std = np.float64(std)
    tmp = std.flatten() ** -2
    exp = [np.roots([1, 7, 16 - t, 12 - t]).real.max() for t in tmp]
    exp = np.float64(exp).reshape(std.shape)
    return exp

def power_function_correlation(a_ofs, a_std, b_ofs, b_std):
    a_exp = std_to_exp(a_std)
    b_exp = std_to_exp(b_std)
    t_ratio = a_ofs / b_ofs
    t_exp = np.where(a_ofs < b_ofs, b_exp, -a_exp)
    t_max = np.maximum(a_ofs, b_ofs)
    num = (a_exp + 1) * (b_exp + 1) * t_ratio ** t_exp
    den = (a_exp + b_exp + 1) * t_max
    return num / den


def solve_posthoc_coefficients(in_ofs, in_std, out_ofs, out_std): # => [in, out]
    in_ofs, in_std = np.broadcast_arrays(in_ofs, in_std)
    out_ofs, out_std = np.broadcast_arrays(out_ofs, out_std)
    rv = lambda x: np.float64(x).reshape(-1, 1)
    cv = lambda x: np.float64(x).reshape(1, -1)
    A = power_function_correlation(rv(in_ofs), rv(in_std), cv(in_ofs), cv(in_std))
    B = power_function_correlation(rv(in_ofs), rv(in_std), cv(out_ofs), cv(out_std))
    X = np.linalg.solve(A, B)
    X = X / np.sum(X, axis=0)
    return X


class EMARunner():
    def __init__(self, params, workspace, wandb_run, checkpoint_dir):
        self.sorted_params = sorted(params, key=lambda x: (x[0], x[1]))

        self.steps = np.array([x[0] for x in self.sorted_params])
        self.in_stds = np.array([x[1] for x in self.sorted_params])
        self.latest = np.array([workspace.global_step])

        self.checkpoint_dir = checkpoint_dir
        self.workspace = workspace
        self.last_model = copy.deepcopy(workspace.model)
        self.wandb_run = wandb_run

        self.wandb_run.define_metric("std")
        self.wandb_run.define_metric("ema/*", step_metric="std")

    def __call__(self, std):

        out_stds = np.array([std])

        coef = solve_posthoc_coefficients(self.steps, self.in_stds, self.latest, out_stds) 

        ema = self.workspace.ema.get()[0]
        out = copy.deepcopy(ema)
        for p in out.parameters():
            p.zero_()


        out.to(torch.device('cuda:0'))
        for params, c in zip(self.sorted_params, coef):
            step, std_in, file = params
            checkpoint = self.checkpoint_dir / file
            with open(checkpoint, 'rb') as f:
                payload = torch.load(f, pickle_module=dill)
            
            self.workspace.ema.load_state_dict(payload['state_dicts']['ema'])
            self.workspace.load_payload(payload, exclude_keys=['_output_dir'])
            cfg = payload['cfg']
            del payload

            idx = cfg.ema.stds.index(std_in)
            ema = self.workspace.ema.get()[idx]
            ema.to(torch.device('cuda:0'))
            print("including", step, std_in, file, c)
            c = torch.tensor(c, device='cuda:0')
            with torch.no_grad():
                for p, ema_p in zip(out.parameters(), ema.parameters()):
                    p += ema_p * c

        new_state_dict = {"stds": out_stds, 'averaged_models': [out.state_dict() for i in range(2)]}
        
        self.workspace.ema.load_state_dict(new_state_dict)

        policy = self.workspace.ema.get()[0]
        for p_net, p_ema in zip(self.last_model.buffers(), policy.buffers()):
            p_ema.copy_(p_net)

        policy.to(torch.device('cuda:0'))
        policy.eval()

        env_runner = hydra.utils.instantiate(
            self.workspace.cfg.task.env_runner,
            output_dir=self.checkpoint_dir)

        runner_log = env_runner.run(policy)
        print(f"std: {std}, score: {runner_log['test/mean_score']}")
        runner_log = {f"ema/{'_'.join(k.split('/'))}": v for k, v in runner_log.items()} # prefix keys with ema/
        runner_log["std"] = std
        self.wandb_run.log(runner_log)

        return runner_log["ema/test_mean_score"]

@click.command()
@click.option('-w', '--workspace_dir', required=True)
@click.option('-n', '--n_iter', default=10)
def main(workspace_dir, n_iter):
    checkpoint_dir = Path(workspace_dir) / "checkpoints"

    checkpoint = checkpoint_dir / "latest.ckpt"

    assert checkpoint.exists(), f"Checkpoint {checkpoint} does not exist"

    with open(checkpoint, 'rb') as f:
        payload = torch.load(f, pickle_module=dill)
    cfg = payload['cfg']
    cls = hydra.utils.get_class(cfg._target_)
    workspace: BaseWorkspace = cls(cfg, output_dir=workspace_dir)
    workspace.load_payload(payload, exclude_keys=['_output_dir'])
    del payload
    saved_stds = cfg.ema.stds
    
    params = []
    for file in checkpoint_dir.iterdir():
        match = re.match(r"epoch_(\d+).ckpt", file.name)
        if not match:
            continue
        checkpoint = checkpoint_dir / file
        with open(checkpoint, 'rb') as f:
            payload = torch.load(f, pickle_module=dill)
        cfg = payload['cfg']
        global_step = dill.loads(payload['pickles']['global_step'])
        model_params1 = (global_step, cfg.ema.stds[0], file.name)
        model_params2 = (global_step, cfg.ema.stds[1], file.name)
        params.append(model_params1)
        params.append(model_params2)
        del payload

    # add latest checkpoint
    model_params1 = (workspace.global_step, saved_stds[0], "latest.ckpt")
    model_params2 = (workspace.global_step, saved_stds[1], "latest.ckpt")
    params.append(model_params1)
    params.append(model_params2)
    wandb_run = wandb.init(
        dir=str(workspace_dir),
        **cfg.logging,
    )

    ema_runner = EMARunner(params, workspace, wandb_run, checkpoint_dir)

    pbounds = {'std': (0.01, 0.25)}
    optimizer = BayesianOptimization(
        f=ema_runner,
        pbounds=pbounds,
        verbose=2, # verbose = 1 prints only when a maximum is observed, verbose = 0 is silent
        random_state=1,
    )
    optimizer.set_gp_params(alpha=1e-3)
    acquisition = UtilityFunction(kind="ucb", kappa=8, xi=None)
    optimizer.maximize(
        init_points=3,
        n_iter=n_iter,
        acquisition_function=acquisition,
    )
    print(optimizer.max)
    

if __name__ == '__main__':
    main()