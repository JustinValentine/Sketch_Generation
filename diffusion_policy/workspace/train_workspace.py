if __name__ == "__main__":
    import sys
    import os
    import pathlib

    ROOT_DIR = str(pathlib.Path(__file__).parent.parent.parent)
    sys.path.append(ROOT_DIR)
    os.chdir(ROOT_DIR)

import os
import hydra
import torch
from omegaconf import OmegaConf, DictConfig
import pathlib
from torch.utils.data import DataLoader, RandomSampler
import copy
import random
import wandb
import tqdm
import numpy as np
from diffusion_policy.workspace.base_workspace import BaseWorkspace
from diffusion_policy.policy.base_policy import BasePolicy
from diffusion_policy.dataset.base_dataset import BaseDataset
from diffusion_policy.env_runner.base_runner import BaseRunner
from diffusion_policy.common.checkpoint_util import TopKCheckpointManager
from diffusion_policy.common.json_logger import JsonLogger
from diffusion_policy.common.pytorch_util import dict_apply, optimizer_to
from diffusion_policy.model.diffusion.ema_model import EMAModel
from diffusion_policy.model.common.lr_scheduler import get_scheduler

OmegaConf.register_new_resolver("eval", eval, replace=True)


class TrainWorkspace(BaseWorkspace):
    include_keys = ["global_step", "epoch"]

    def __init__(self, cfg: DictConfig, output_dir=None):
        super().__init__(cfg, output_dir=output_dir)

        # set seed
        seed = cfg.training.seed
        torch.manual_seed(seed)
        np.random.seed(seed)
        random.seed(seed)

        # configure model
        self.model: BasePolicy = hydra.utils.instantiate(cfg.policy)

        self.ema: EMAModel = None
        if cfg.training.use_ema:
            # ensure normalized and device set before passing in model to ema
            self.ema = hydra.utils.instantiate(cfg.ema, model=self.model)

        # configure training state
        self.optimizer = self.model.get_optimizer(cfg.optimizer)

        # configure training state
        self.global_step = 0
        self.epoch = 0

    def run(self):
        cfg = copy.deepcopy(self.cfg)

        if cfg.training.debug:
            cfg.training.num_epochs = 1
            cfg.training.max_train_steps = 3
            cfg.training.max_val_steps = 3
            cfg.training.rollout_every = 1
            cfg.training.checkpoint_every = 1
            cfg.training.val_every = 1
            cfg.training.sample_every = 1
            cfg.dataloader.batch_size = 4
            cfg.val_dataloader.batch_size = 4
            cfg.checkpoint.save_last_ckpt = True
            cfg.checkpoint.save_last_snapshot = False
            cfg.checkpoint.topk.k = 1
            cfg.logging.mode = "offline"

            if "n_train" in cfg.task.env_runner.keys():
                cfg.task.env_runner.n_train = 1
                cfg.task.env_runner.n_train_vis = 1
                cfg.task.env_runner.n_test = 1
                cfg.task.env_runner.n_test_vis = 1
                cfg.task.env_runner.max_steps = 8
                cfg.task.env_runner.n_envs = None


        # configure dataset
        dataset: BaseDataset
        dataset = hydra.utils.instantiate(cfg.task.dataset)
        assert isinstance(dataset, BaseDataset)
        train_dataloader = DataLoader(dataset, **cfg.dataloader)
        normalizer = dataset.get_normalizer()

        # configure validation dataset
        val_dataset = dataset.get_validation_dataset()
        val_dataloader = DataLoader(val_dataset, **cfg.val_dataloader)

        val_sampler = RandomSampler(val_dataset, replacement=True)
        val_sampler_dataloader = DataLoader(
            val_dataset, sampler=val_sampler, batch_size=cfg.dataloader.batch_size
        )

        self.model.set_normalizer(normalizer)

        # resume training
        if cfg.training.resume:
            lastest_ckpt_path = self.get_checkpoint_path()
            if lastest_ckpt_path.is_file():
                print(f"Resuming from checkpoint {lastest_ckpt_path}")
                self.load_checkpoint(path=lastest_ckpt_path)
                if cfg.logging.resume:
                     cfg.logging.id = self.cfg.logging.id

        # device transfer
        device = torch.device(cfg.training.device)
        self.model.to(device)
        # self.model = torch.compile(self.model, mode="reduce-overhead")
        # self.ema = hydra.utils.instantiate(cfg.ema, model=self.model)
        self.ema.to(device)

        # configure lr scheduler
        lr_scheduler = get_scheduler(
            cfg.training.lr_scheduler,
            optimizer=self.optimizer,
            num_warmup_steps=cfg.training.lr_warmup_steps,
            num_training_steps=(len(train_dataloader) * cfg.training.num_epochs)
            // cfg.training.gradient_accumulate_every,
            # pytorch assumes stepping LRScheduler every epoch
            # however huggingface diffusers steps it every batch
            last_epoch=self.global_step - 1,
        )

        # configure env
        env_runner: BaseRunner
        env_runner = hydra.utils.instantiate(
            cfg.task.env_runner, output_dir=self.output_dir
        )
        assert isinstance(env_runner, BaseRunner)

        # configure logging
        wandb_run = wandb.init(
            dir=str(self.output_dir),
            config=OmegaConf.to_container(cfg, resolve=True),
            **cfg.logging,
        )
        wandb.config.update(
            {
                "output_dir": self.output_dir,
            }
        )
        run_id = wandb.run.id
        self.cfg.logging.id = run_id

        optimizer_to(self.optimizer, device)

        # save batch for sampling
        train_sampling_batch = None
        

        # training loop
        log_path = os.path.join(self.output_dir, "logs.json.txt")
        with JsonLogger(log_path) as json_logger:
            for local_epoch_idx in range(cfg.training.num_epochs):
                step_log = dict()
                # ========= train for this epoch ==========
                if (
                    "freeze_encoder" in cfg.training.keys()
                    and cfg.training.freeze_encoder
                ):
                    self.model.obs_encoder.eval()
                    self.model.obs_encoder.requires_grad_(False)

                self.model.train()
                with tqdm.tqdm(
                    train_dataloader,
                    desc=f"Training epoch {self.epoch}",
                    leave=False,
                    mininterval=cfg.training.tqdm_interval_sec,
                ) as tepoch:
                    for batch_idx, batch in enumerate(tepoch):
                        # device transfer
                        batch = dict_apply(
                            batch, lambda x: x.to(device, non_blocking=True)
                        )
                        if train_sampling_batch is None:
                            train_sampling_batch = batch

                        # compute loss
                        raw_loss = self.model.compute_loss(batch)
                        loss = raw_loss / cfg.training.gradient_accumulate_every
                        loss.backward()

                        # step optimizer
                        if (
                            self.global_step % cfg.training.gradient_accumulate_every
                            == 0
                        ):
                            self.optimizer.step()
                            self.optimizer.zero_grad()
                            lr_scheduler.step()

                        # update ema
                        if cfg.training.use_ema:
                            self.ema.step(self.model)

                        # logging
                        raw_loss_cpu = raw_loss.item()
                        tepoch.set_postfix(loss=raw_loss_cpu, refresh=False)
                        step_log = {
                            "train_loss": raw_loss_cpu,
                            "global_step": self.global_step,
                            "epoch": self.epoch,
                            "lr": lr_scheduler.get_last_lr()[0],
                        }

                        is_last_batch = batch_idx == (len(train_dataloader) - 1)
                        if not is_last_batch:
                            # log of last step is combined with validation and rollout
                            wandb_run.log(step_log, step=self.global_step)
                            json_logger.log(step_log)
                            self.global_step += 1

                        if (cfg.training.max_train_steps is not None) and batch_idx >= (
                            cfg.training.max_train_steps - 1
                        ):
                            break

                # ========= eval for this epoch ==========
                policy = self.model
                if cfg.training.use_ema:
                    policy = self.ema.get()
                    if isinstance(policy, list):
                        policy = policy[0]
                policy.set_normalizer(normalizer)
                policy.to(device)
                policy.eval()

                # run rollout
                if (self.epoch % cfg.training.rollout_every) == 0:
                    runner_log = env_runner.run(policy)
                    # log all
                    step_log.update(runner_log)

                # run validation
                if (self.epoch % cfg.training.val_every) == 0:
                    self.model.eval()
                    with torch.no_grad():
                        val_losses = list()
                        with tqdm.tqdm(
                            val_dataloader,
                            desc=f"Validation epoch {self.epoch}",
                            leave=False,
                            mininterval=cfg.training.tqdm_interval_sec,
                        ) as tepoch:
                            for batch_idx, batch in enumerate(tepoch):
                                batch = dict_apply(
                                    batch, lambda x: x.to(device, non_blocking=True)
                                )

                                loss = self.model.compute_loss(batch)
                                val_losses.append(loss)
                                if (
                                    cfg.training.max_val_steps is not None
                                ) and batch_idx >= (cfg.training.max_val_steps - 1):
                                    break
                        if len(val_losses) > 0:
                            val_loss = torch.mean(torch.tensor(val_losses)).item()
                            # log epoch average validation loss
                            step_log["val_loss"] = val_loss

                # run diffusion sampling on a training batch
                if (self.epoch % cfg.training.sample_every) == 0:
                    with torch.no_grad():
                        # sample trajectory from training set, and evaluate difference
                        batch = dict_apply(
                            train_sampling_batch,
                            lambda x: x.to(device, non_blocking=True),
                        )
                        obs_dict = {"obs": batch["obs"]}
                        gt_action = batch["action"]

                        result = policy.predict_action(obs_dict)
                        pred_action = result["action_pred"]
                        mse = torch.nn.functional.mse_loss(pred_action, gt_action)
                        step_log["train_action_mse_error"] = mse.item()
                        del batch
                        del obs_dict
                        del gt_action
                        del result
                        del pred_action
                        del mse
                        # sample trajectory from val set, and evaluate difference
                        for val_sampling_batch in val_sampler_dataloader:
                            batch = dict_apply(
                                val_sampling_batch,
                                lambda x: x.to(device, non_blocking=True),
                            )
                            # TODO: fix so lowdim dataloader outputs dict
                            obs_dict = {"obs": batch["obs"]}
                            gt_action = batch["action"]

                            result = policy.predict_action(obs_dict)
                            pred_action = result["action_pred"]
                            mse = torch.nn.functional.mse_loss(pred_action, gt_action)
                            step_log["val_action_mse_error"] = mse.item()
                            del batch
                            del obs_dict
                            del gt_action
                            del result
                            del pred_action
                            del mse

                            # single batch
                            break

                if cfg.checkpoint.save_last_ckpt:
                    self.save_checkpoint()

                if cfg.checkpoint.save_last_snapshot:
                    self.save_snapshot()

                # checkpoint
                if (self.epoch % cfg.training.checkpoint_every) == 0:
                    self.save_checkpoint(tag=f"epoch_{self.epoch}")

                policy.train()

                # end of epoch
                # log of last step is combined with validation and rollout
                wandb_run.log(step_log, step=self.global_step)
                json_logger.log(step_log)
                self.global_step += 1
                self.epoch += 1

            env_runner.close()


@hydra.main(
    version_base=None,
    config_path=str(pathlib.Path(__file__).parent.parent.joinpath("config")),
    config_name=pathlib.Path(__file__).stem,
)
def main(cfg):
    workspace = TrainWorkspace(cfg)
    workspace.run()


if __name__ == "__main__":
    main()
