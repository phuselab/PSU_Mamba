# Copyright (c) MONAI Consortium
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compusernce with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

import contextlib
import io
import logging
import math
import os
import random
import sys
import time
import warnings
from datetime import datetime
from typing import Optional, Sequence, Union

import mlflow
import mlflow.pytorch
import numpy as np
import torch
import torch.distributed as dist
import yaml
from torch.nn.parallel import DistributedDataParallel
from torch.nn.utils import clip_grad_norm_
from torch.utils.tensorboard import SummaryWriter
from tqdm import tqdm

import monai
from monai import transforms
from monai.apps import download_url
from monai.apps.auto3dseg.auto_runner import logger
from monai.apps.utils import DEFAULT_FMT
from monai.auto3dseg.utils import datafold_read
from monai.bundle import ConfigParser
from monai.bundle.scripts import _pop_args, _update_args
from monai.data import DataLoader, partition_dataset
from monai.inferers import sliding_window_inference
from monai.metrics import compute_dice, HausdorffDistanceMetric
from monai.utils import RankFilter, set_determinism

from Code_general_functions.extract_reference_label import get_reference_label_path, get_reference_label_paths    


if __package__ in (None, ""):
    from algo import auto_scale
else:
    from .algo import auto_scale

CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"monai_default": {"format": DEFAULT_FMT}},
    "loggers": {
        "monai.apps.auto3dseg.auto_runner": {"handlers": ["file", "console"], "level": "DEBUG", "propagate": False}
    },
    "filters": {"rank_filter": {"()": RankFilter}},
    "handlers": {
        "file": {
            "class": "logging.FileHandler",
            "filename": "runner.log",
            "mode": "a",  # append or overwrite
            "level": "DEBUG",
            "formatter": "monai_default",
            "filters": ["rank_filter"],
        },
        "console": {
            "class": "logging.StreamHandler",
            "level": "INFO",
            "formatter": "monai_default",
            "filters": ["rank_filter"],
        },
    },
}


class EarlyStopping:
    def __init__(self, patience=5, delta=0, verbose=False):
        self.patience = patience
        self.delta = delta
        self.verbose = verbose
        self.counter = 0
        self.best_score = None
        self.early_stop = False
        self.val_acc_max = -1

    def __call__(self, val_acc):
        if self.best_score is None:
            self.best_score = val_acc
        elif val_acc + self.delta < self.best_score:
            self.counter += 1
            if self.verbose:
                logger.debug(f"EarlyStopping counter: {self.counter} out of {self.patience}")
            if self.counter >= self.patience:
                self.early_stop = True
        else:
            self.best_score = val_acc
            self.counter = 0


def pre_operation(config_file, **override):
    """Update the hyper_parameters.yaml based on GPU memory"""
    rank = int(os.getenv("RANK", "0"))
    if rank == 0:
        if isinstance(config_file, str) and "," in config_file:
            config_file = config_file.split(",")

        for _file in config_file:
            if "hyper_parameters.yaml" in _file:
                parser = ConfigParser(globals=False)
                parser.read_config(_file)
                auto_scale_allowed = override.get("auto_scale_allowed", parser["auto_scale_allowed"])
                max_epoch = override.get("auto_scale_max_epochs", parser["auto_scale_max_epochs"])
                if auto_scale_allowed:
                    output_classes = parser["training#output_classes"]
                    n_cases = parser["training#n_cases"]
                    scaled = auto_scale(output_classes, n_cases, max_epoch)
                    parser.update({"num_patches_per_iter": scaled["num_patches_per_iter"]})
                    parser.update({"num_crops_per_image": scaled["num_crops_per_image"]})
                    parser.update({"num_epochs": scaled["num_epochs"]})
                    ConfigParser.export_config_file(parser.get(), _file, fmt="yaml", default_flow_style=None)
    return


def run(config_file: Optional[Union[str, Sequence[str]]] = None, **override):
    # Initialize distributed and scale parameters based on GPU memory
    if torch.cuda.device_count() > 1:
        dist.init_process_group(backend="nccl", init_method="env://")
        world_size = dist.get_world_size()
        pre_operation(config_file, **override)
        dist.barrier()          # ensures that all processes have finished the pre_operation
    else:
        pre_operation(config_file, **override)
        world_size = 1

    logging.basicConfig(stream=sys.stdout, level=logging.INFO)

    if isinstance(config_file, str) and "," in config_file:
        config_file = config_file.split(",")

    _args = _update_args(config_file=config_file, **override)
    config_file_ = _pop_args(_args, "config_file")[0]

    parser = ConfigParser()
    parser.read_config(config_file_)
    parser.update(pairs=_args)

    amp = parser.get_parsed_content("training#amp")
    bundle_root = parser.get_parsed_content("bundle_root")
    ckpt_path = parser.get_parsed_content("ckpt_path")
    data_file_base_dir = parser.get_parsed_content("data_file_base_dir")
    data_list_file_path = parser.get_parsed_content("data_list_file_path")
    finetune = parser.get_parsed_content("finetune")
    fold = parser.get_parsed_content("fold")
    mlflow_tracking_uri = parser.get_parsed_content("mlflow_tracking_uri")
    mlflow_experiment_name = parser.get_parsed_content("mlflow_experiment_name")
    num_images_per_batch = parser.get_parsed_content("training#num_images_per_batch")
    num_iterations = parser.get_parsed_content("training#num_iterations")
    num_iterations_per_validation = parser.get_parsed_content("num_epochs_per_validation")
    num_sw_batch_size = parser.get_parsed_content("training#num_sw_batch_size")
    num_patches_per_iter = parser.get_parsed_content("training#num_patches_per_iter")
    output_classes = parser.get_parsed_content("training#output_classes")
    overlap_ratio = parser.get_parsed_content("training#overlap_ratio")
    overlap_ratio_final = parser.get_parsed_content("training#overlap_ratio_final")
    roi_size_valid = parser.get_parsed_content("training#patch_size_valid")
    random_seed = parser.get_parsed_content("random_seed")
    sw_input_on_cpu = parser.get_parsed_content("training#sw_input_on_cpu")
    softmax = parser.get_parsed_content("training#softmax")
    valid_at_orig_resolution_at_last = parser.get_parsed_content("training#valid_at_orig_resolution_at_last")
    valid_at_orig_resolution_only = parser.get_parsed_content("training#valid_at_orig_resolution_only")
    use_pretrain = parser.get_parsed_content("use_pretrain")
    pretrained_path = parser.get_parsed_content("pretrained_path")
    pin_memory = parser.get_parsed_content("training#pin_memory")

    if not valid_at_orig_resolution_only:
        train_transforms = parser.get_parsed_content("transforms_train")
        val_transforms = parser.get_parsed_content("transforms_validate")

    if valid_at_orig_resolution_at_last or valid_at_orig_resolution_only:
        infer_transforms = parser.get_parsed_content("transforms_infer")
        infer_transforms = transforms.Compose(
            [
                infer_transforms,
                transforms.LoadImaged(keys=["label", "ref_label"], image_only=False),
                transforms.EnsureChannelFirstd(keys=["label", "ref_label"]),
                transforms.EnsureTyped(keys=["label", "ref_label"]),
            ]
        )

        if "class_names" in parser and isinstance(parser["class_names"], list) and "index" in parser["class_names"][0]:
            class_index = [x["index"] for x in parser["class_names"]]

            infer_transforms = transforms.Compose(
                [
                    infer_transforms,
                    transforms.Lambdad(
                        keys=["label", "ref_label"],
                        func=lambda x: torch.cat([sum([x == i for i in c]) for c in class_index], dim=0).to(
                            dtype=x.dtype
                        ),
                    ),
                ]
            )

    class_names = None
    try:
        class_names = parser.get_parsed_content("class_names")
    except BaseException:
        pass

    # Adaptation and early stopping
    ad = parser.get_parsed_content("adapt_valid_mode")
    if ad:
        ad_progress_percentages = parser.get_parsed_content("adapt_valid_progress_percentages") # list of percentages indicating the progress at which validation should be performed
        ad_num_epochs_per_validation = parser.get_parsed_content("adapt_valid_num_epochs_per_validation") # ist of numbers indicating the number of epochs to be performed for each validation.
        
        # Sort the lists in ascending order of progress percentages
        sorted_indices = np.argsort(ad_progress_percentages)
        ad_progress_percentages = [ad_progress_percentages[_i] for _i in sorted_indices]
        ad_num_epochs_per_validation = [ad_num_epochs_per_validation[_i] for _i in sorted_indices]

    # Early stopping
    es = parser.get_parsed_content("training#early_stop_mode")
    if es:
        es_delta = parser.get_parsed_content("training#early_stop_delta")
        es_patience = parser.get_parsed_content("training#early_stop_patience")

    if not os.path.exists(ckpt_path):
        os.makedirs(ckpt_path, exist_ok=True)

    # Set up logging
    if random_seed is not None and (isinstance(random_seed, int) or isinstance(random_seed, float)):
        set_determinism(seed=random_seed)

    CONFIG["handlers"]["file"]["filename"] = parser.get_parsed_content("log_output_file")
    print("log_output_file: ", parser.get_parsed_content("log_output_file"))
    logging.config.dictConfig(CONFIG)
    print("CONFIG: ", CONFIG)
    logging.getLogger("torch.distributed.distributed_c10d").setLevel(logging.WARNING)
    logger.debug(f"Number of GPUs: {torch.cuda.device_count()}")
    logger.debug(f"World_size: {world_size}")
    logger.debug(f"Batch size: {num_images_per_batch}")

    datalist = ConfigParser.load_config_file(data_list_file_path)

    # Get reference label path if available, else throw error message
    if "data_benchmark_base_dir" in datalist:
        data_benchmark_base_dir = datalist["data_benchmark_base_dir"]
    else:
        raise ValueError("data_benchmark_base_dir not found in data_list_file_path")

    # Data loading
    train_files, val_files = datafold_read(datalist=data_list_file_path, basedir=data_file_base_dir, fold=fold)

    # get list of all image paths of the train_files list 
    #str_imgs_train = [os.path.join(data_file_base_dir, item['image']) for item in train_files]
    str_imgs_train = [os.path.join(data_file_base_dir, item['image'][0] if isinstance(item['image'], list) else item['image'])for item in train_files if item['image']]
    str_imgs_val = [os.path.join(data_file_base_dir, item['image'][0] if isinstance(item['image'], list) else item['image']) for item in val_files if item['image']]
    #str_imgs_val = [os.path.join(data_file_base_dir, item['image']) for item in val_files]
    
    # T1xFLAIR img-seg comparison
    str_ref_seg_tr = get_reference_label_paths(str_imgs_train, data_benchmark_base_dir)
    str_ref_seg_val = get_reference_label_paths(str_imgs_val, data_benchmark_base_dir)
    
     # Add reference label to train_files and val_files
    for item, str_ref_t in zip(train_files, str_ref_seg_tr):
        item["ref_label"] = str_ref_t

    for item, str_ref_v in zip(val_files, str_ref_seg_val):
        item["ref_label"] = str_ref_v

    # Use seed here for reproducibility 
    random.shuffle(train_files)

    if torch.cuda.device_count() > 1:
        train_files = partition_dataset(data=train_files, shuffle=True, num_partitions=world_size, even_divisible=True)[
            dist.get_rank()
        ]
    logger.debug(f"Train_files: {len(train_files)}")

    if torch.cuda.device_count() > 1:
        if len(val_files) < world_size:
            # If the number of validation files is less than the number of GPUs, replicate the validation files
            val_files = val_files * math.ceil(float(world_size) / float(len(val_files)))
        # Partition the validation files
        val_files = partition_dataset(data=val_files, 
                                      shuffle=False, 
                                      num_partitions=world_size, 
                                      even_divisible=False)[
            dist.get_rank()
        ]
    logger.debug(f"validation_files: {len(val_files)}")

    # Data loading
    train_cache_rate = float(parser.get_parsed_content("train_cache_rate"))
    validate_cache_rate = float(parser.get_parsed_content("validate_cache_rate"))

    with warnings.catch_warnings():
        warnings.simplefilter(action="ignore", category=FutureWarning)
        warnings.simplefilter(action="ignore", category=Warning)
        # Cache the training and validation datasets
        if not valid_at_orig_resolution_only:
            train_ds = monai.data.CacheDataset(
                data=train_files * num_iterations_per_validation,
                transform=train_transforms,
                cache_rate=train_cache_rate,
                hash_as_key=True,
                num_workers=parser.get_parsed_content("num_cache_workers"),
                progress=parser.get_parsed_content("show_cache_progress"),
            )
            val_ds = monai.data.CacheDataset(
                data=val_files,
                transform=val_transforms,
                cache_rate=validate_cache_rate,
                hash_as_key=True,
                num_workers=parser.get_parsed_content("num_cache_workers"),
                progress=parser.get_parsed_content("show_cache_progress"),
            )
        # Cache the validation dataset at original resolution
        if valid_at_orig_resolution_at_last or valid_at_orig_resolution_only:
            orig_val_ds = monai.data.Dataset(data=val_files, transform=infer_transforms)

    # Data loading
    if not valid_at_orig_resolution_only:
        train_loader = DataLoader(
            train_ds,
            num_workers=parser.get_parsed_content("num_workers"),
            batch_size=num_images_per_batch,
            shuffle=True,
            persistent_workers=True,
            pin_memory=pin_memory,
        )
        val_loader = DataLoader(
            val_ds, 
            num_workers=parser.get_parsed_content("num_workers_validation"), 
            batch_size=1, 
            shuffle=False
        )

    # Data loading
    if valid_at_orig_resolution_at_last or valid_at_orig_resolution_only:
        orig_val_loader = DataLoader(
            orig_val_ds, 
            num_workers=parser.get_parsed_content("num_workers_validation"), 
            batch_size=1, 
            shuffle=False
        )

    device = torch.device(f"cuda:{os.environ['LOCAL_RANK']}") if world_size > 1 else torch.device("cuda:0")

    with io.StringIO() as buffer, contextlib.redirect_stdout(buffer):
        model = parser.get_parsed_content("network")
    model = model.to(device)

    # Load the pretrained weights
    if use_pretrain:
        if torch.cuda.device_count() == 1 or dist.get_rank() == 0:
            download_url(
                url="https://api.ngc.nvidia.com/v2/models/nvidia/monaihosting/swin_unetr_pretrained/versions/1.0/files/swin_unetr.base_5000ep_f48_lr2e-4_pretrained.pt",
                filepath=pretrained_path,
                progress=False,
            )
        if torch.cuda.device_count() > 1:
            dist.barrier()
        # Check periodically until the file is ready
        timeout = 60  # maximum time to wait (in seconds)
        start_time = time.time()  # remember when we started
        while not os.path.exists(pretrained_path):
            time.sleep(1)
            if time.time() - start_time > timeout:  # timeout limit reached
                raise TimeoutError(
                    f"Pretrained weights file could not be found at {pretrained_path} after waiting for {timeout} seconds"
                )
        # Load the pretrained weights
        store_dict = model.state_dict()
        model_dict = torch.load(pretrained_path, map_location=device)["state_dict"]
        for key in model_dict.keys():
            if "out" not in key:
                store_dict[key].copy_(model_dict[key])
        model.load_state_dict(store_dict)
        logger.debug("Using pretrained weights")

    # Set up the model
    if torch.cuda.device_count() > 1:
        # Convert the model to use synchronized batch normalization to avoid the "process group has no NCCL id set" error
        model = torch.nn.SyncBatchNorm.convert_sync_batchnorm(model)

    if softmax:
        post_pred = transforms.Compose([transforms.EnsureType(), transforms.AsDiscrete(argmax=True)])
    else:
        post_pred = transforms.Compose(
            [transforms.EnsureType(), transforms.Activations(sigmoid=True), transforms.AsDiscrete(threshold=0.5)]
        )

    # transform for validation at original resolution
    if valid_at_orig_resolution_at_last or valid_at_orig_resolution_only:
        post_transforms = [
            transforms.Invertd(
                keys="pred",
                transform=infer_transforms,
                orig_keys="image",
                meta_keys="pred_meta_dict",
                orig_meta_keys="image_meta_dict",
                meta_key_postfix="meta_dict",
                nearest_interp=False,
                to_tensor=True,
            )
        ]
        if softmax: # if softmax, use argmax
            post_transforms += [transforms.AsDiscreted(keys="pred", argmax=True)]
        else: # if not softmax, use threshold
            post_transforms = (
                [transforms.Activationsd(keys="pred", sigmoid=True)]
                + post_transforms
                + [transforms.AsDiscreted(keys="pred", threshold=0.5)]
            )
        post_transforms = transforms.Compose(post_transforms)

    # Set up the optimizer, learning rate scheduler, and loss function
    loss_function = parser.get_parsed_content("training#loss")

    optimizer_part = parser.get_parsed_content("training#optimizer", instantiate=False)
    optimizer = optimizer_part.instantiate(params=model.parameters())

    num_epochs_per_validation = num_iterations_per_validation // len(train_loader)
    num_epochs_per_validation = max(num_epochs_per_validation, 1)
    num_epochs = num_epochs_per_validation * (num_iterations // num_iterations_per_validation)


    if torch.cuda.device_count() == 1 or dist.get_rank() == 0:
        logger.debug(f"num_epochs: {num_epochs}")
        logger.debug(f"num_epochs_per_validation: {num_epochs_per_validation}")
        logger.debug(f"num_iterations: {num_iterations}")
        logger.debug(f"num_iterations_per_validation: {num_iterations_per_validation}")

    lr_scheduler_part = parser.get_parsed_content("training#lr_scheduler", instantiate=False)
    lr_scheduler = lr_scheduler_part.instantiate(optimizer=optimizer)

    if torch.cuda.device_count() > 1:
        # Wrap the model with DistributedDataParallel to use synchronized batch normalization
        model = DistributedDataParallel(model, device_ids=[device], find_unused_parameters=False)

    if finetune["activate"] and os.path.isfile(finetune["pretrained_ckpt_name"]):
        logger.debug("Fine-tuning pre-trained checkpoint {:s}".format(finetune["pretrained_ckpt_name"]))
        # Load the pre-trained checkpoint
        if torch.cuda.device_count() > 1:
            model.module.load_state_dict(torch.load(finetune["pretrained_ckpt_name"], map_location=device))
        else:
            model.load_state_dict(torch.load(finetune["pretrained_ckpt_name"], map_location=device))
    else:
        if not use_pretrain:
            logger.debug("Training from scratch")
    
    # Set up the training loop
    if amp:
        from torch.cuda.amp import GradScaler, autocast

        scaler = GradScaler()
        if torch.cuda.device_count() == 1 or dist.get_rank() == 0:
            logger.debug("Amp enabled")

    # Initialize the best metric and the best metric epoch
    best_metric = -1; best_hd_metric = 3000
    best_metric_epoch = -1; best_hd_metric_epoch = -1
    idx_iter = 0
    metric_dim = output_classes - 1 if softmax else output_classes

    # Initialize the Hausdorff distance metric
    compute_hausdorff_distance_95 = HausdorffDistanceMetric(include_background=False, percentile=95)

    val_devices_input = {}
    val_devices_output = {}

    if es: # early stopping
        stop_train = torch.tensor(False).to(device)

    # Set up the logging
    if torch.cuda.device_count() == 1 or dist.get_rank() == 0:
        writer = SummaryWriter(log_dir=os.path.join(ckpt_path, "Events"))
        # Set up MLflow logging
        mlflow.set_tracking_uri(mlflow_tracking_uri)
        # Set the experiment
        mlflow.set_experiment(mlflow_experiment_name)
        # Start the run
        mlflow.start_run(run_name=f"swinunetr - fold{fold} - train")
        with open(os.path.join(ckpt_path, "accuracy_history.csv"), "a") as f:
            f.write("epoch\tmetric\tmetric_ref\thd_metric\thd_metric_ref\tloss\tloss_ref\tlr\ttime\titer\n")

        if es:
            # instantiate the early stopping object
            early_stopping = EarlyStopping(patience=es_patience, delta=es_delta, verbose=True)

    start_time = time.time()

    # To increase speed, the training script is not based on epoch, but based on validation rounds.
    # In each batch, num_images_per_batch=2 whole 3D images are loaded into CPU for data transformation
    # num_crops_per_image=2*num_patches_per_iter is extracted from each 3D image, in each iteration,
    # num_patches_per_iter patches is used for training (real batch size on each GPU).
    # The training script will run for num_epochs, and in each epoch, num_epochs_per_validation epochs are run.

    # num_rounds is the number of validation rounds
    num_rounds = int(np.ceil(float(num_epochs) // float(num_epochs_per_validation)))
    if num_rounds == 0:
        raise RuntimeError("num_epochs_per_validation > num_epochs, modify hyper_parameters.yaml")

    with warnings.catch_warnings():
        warnings.simplefilter(action="ignore", category=FutureWarning)
        warnings.simplefilter(action="ignore", category=Warning)
        if not valid_at_orig_resolution_only:
            if torch.cuda.device_count() == 1 or dist.get_rank() == 0:
                progress_bar = tqdm(
                    range(num_rounds), desc=f"{os.path.basename(bundle_root)} - training ...", unit="round"
                )
            # Training loop
            for _round in range(num_rounds) if torch.cuda.device_count() > 1 and dist.get_rank() != 0 else progress_bar:
                # Set the epoch
                epoch = (_round + 1) * num_epochs_per_validation
                # Set the learning rate
                lr = lr_scheduler.get_last_lr()[0]
                if torch.cuda.device_count() == 1 or dist.get_rank() == 0:
                    logger.debug("----------")
                    logger.debug(f"epoch {_round * num_epochs_per_validation + 1}/{num_epochs}")
                    logger.debug(f"Learning rate is set to {lr}")

                # Set the model to training mode
                model.train()
                epoch_loss = 0
                epoch_loss_t1xflair = 0 # also track loss for T1xFLAIR, not used for backpropagation
                loss_torch = torch.zeros(2, dtype=torch.float, device=device)
                loss_torch_t1xflair = torch.zeros(2, dtype=torch.float, device=device)
                step = 0    # step is the number of iterations

                for batch_data in train_loader:
                    step += 1

                    # _l as it is a list of images?
                    inputs_l = (    # inputs_l are the input images
                        batch_data["image"].as_tensor()
                        if isinstance(batch_data["image"], monai.data.MetaTensor)
                        else batch_data["image"]
                    )
                    labels_l = (
                        batch_data["label"].as_tensor()
                        if isinstance(batch_data["label"], monai.data.MetaTensor)
                        else batch_data["label"]
                    )
                    ref_labels_l = (
                        batch_data["ref_label"].as_tensor()
                        if isinstance(batch_data["ref_label"], monai.data.MetaTensor)
                        else batch_data["ref_label"]
                    )

                    _idx = torch.randperm(inputs_l.shape[0]) # : set seed for reproducibility
                    inputs_l = inputs_l[_idx]
                    labels_l = labels_l[_idx]
                    ref_labels_l = ref_labels_l[_idx]
            
                    for _k in range(inputs_l.shape[0] // num_patches_per_iter):
                        inputs = inputs_l[_k * num_patches_per_iter : (_k + 1) * num_patches_per_iter, ...]
                        labels = labels_l[_k * num_patches_per_iter : (_k + 1) * num_patches_per_iter, ...]
                        ref_labels = ref_labels_l[_k * num_patches_per_iter : (_k + 1) * num_patches_per_iter, ...]
                        # shape: num_patches_per_iter, C, D, H, W = 1, 1, 128, 128, 128
                    
                        inputs = inputs.to(device)
                        labels = labels.to(device)
                        ref_labels = ref_labels.to(device)

                        for param in model.parameters():
                            param.grad = None

                        if amp:     # Automatic Mixed Precision
                            with autocast():
                                outputs = model(inputs)
                                loss = loss_function(outputs.float(), labels)   # loss for the current batch
                                ref_loss = loss_function(outputs.float(), ref_labels).detach()   # loss for the reference label
                    

                            scaler.scale(loss).backward()
                            scaler.unscale_(optimizer)
                            clip_grad_norm_(model.parameters(), 0.5)
                            scaler.step(optimizer)
                            scaler.update()
                        else:
                            print("In swinunetr train, not using amp")
                            outputs = model(inputs)
                            loss = loss_function(outputs.float(), labels)
                            ref_loss = loss_function(outputs.float(), ref_labels)
                            
                            loss.backward()
                            clip_grad_norm_(model.parameters(), 0.5)
                            optimizer.step()

                        epoch_loss += loss.item()
                        epoch_loss_t1xflair += ref_loss.item()
                        loss_torch[0] += loss.item()
                        loss_torch[1] += 1.0                # counter for the number of batches
                        loss_torch_t1xflair[0] += ref_loss.item() # loss for T1xFLAIR
                        loss_torch_t1xflair[1] += 1.0
                        epoch_len = len(train_loader)
                        idx_iter += 1

                        if torch.cuda.device_count() == 1 or dist.get_rank() == 0:
                            logger.debug(
                                f"[{str(datetime.now())[:19]}] " + f"{step}/{epoch_len}, train_loss: {loss.item():.4f}, train_loss_ref: {ref_loss.item():.4f}"
                            )
                            writer.add_scalar("train/loss", loss.item(), epoch_len * _round + step)
                            mlflow.log_metric("train/loss", loss.item(), step=epoch_len * _round + step)
                            writer.add_scalar("train/loss_ref", ref_loss.item(), epoch_len * _round + step)
                            mlflow.log_metric("train/loss_ref", ref_loss.item(), step=epoch_len * _round + step)

                lr_scheduler.step()

                if torch.cuda.device_count() > 1:
                    dist.barrier()
                    dist.all_reduce(loss_torch, op=torch.distributed.ReduceOp.SUM)

                loss_torch = loss_torch.tolist()
                loss_torch_t1xflair = loss_torch_t1xflair.tolist()
            
                if torch.cuda.device_count() == 1 or dist.get_rank() == 0:
                    loss_torch_epoch = loss_torch[0] / loss_torch[1]
                    loss_torch_epoch_t1xflair = loss_torch_t1xflair[0] / loss_torch_t1xflair[1]
                    logger.debug(
                        f"Epoch {epoch + 1} average loss: {loss_torch_epoch:.4f}, average loss_T1xFLAIR: {loss_torch_epoch_t1xflair:.4f}, "
                        f"best mean dice: {best_metric:.4f} at epoch {best_metric_epoch}"
                    )

                del inputs, labels, outputs     # clear memory
                torch.cuda.empty_cache()

                if ad:
                    _percentage = float(_round) / float(num_rounds) * 100.0

                    target_num_epochs_per_validation = -1
                    for _j in range(len(ad_progress_percentages)):
                        if _percentage <= ad_progress_percentages[-1 - _j]:
                            if (
                                _j == (len(ad_progress_percentages) - 1)
                                or _percentage > ad_progress_percentages[-2 - _j]
                            ):
                                target_num_epochs_per_validation = ad_num_epochs_per_validation[-1 - _j]
                                break

                    if target_num_epochs_per_validation > 0 and (_round + 1) < num_rounds:
                        if (_round + 1) % (target_num_epochs_per_validation // num_epochs_per_validation) != 0:
                            continue

                model.eval()
                with torch.no_grad():
                    # for metric, index 2*c is the dice for class c, and 2*c + 1 is the not-nan counts for class c
                    metric = torch.zeros(metric_dim * 2, dtype=torch.float, device=device)
                    hd_metric = torch.zeros(metric_dim * 2, dtype=torch.float, device=device)
                    ref_metric = torch.zeros(metric_dim * 2, dtype=torch.float, device=device)
                    ref_hd_metric = torch.zeros(metric_dim * 2, dtype=torch.float, device=device)
                
                    _index = 0
                    for val_data in val_loader:
                        try:
                            val_filename = val_data["image_meta_dict"]["filename_or_obj"][0]
                        except BaseException:
                            val_filename = val_data["image"].meta["filename_or_obj"][0]
                        if sw_input_on_cpu:
                            device_list_input = ["cpu"]
                            device_list_output = ["cpu"]
                        elif val_filename not in val_devices_input or val_filename not in val_devices_output:
                            device_list_input = [device, device, "cpu"]
                            device_list_output = [device, "cpu", "cpu"]
                        elif val_filename in val_devices_input and val_filename in val_devices_output:
                            device_list_input = [val_devices_input[val_filename]]
                            device_list_output = [val_devices_output[val_filename]]

                        for _device_in, _device_out in zip(device_list_input, device_list_output):
                            try:
                                val_outputs = None
                                val_devices_input[val_filename] = _device_in
                                val_devices_output[val_filename] = _device_out
                                with autocast(enabled=amp):
                                    val_outputs = sliding_window_inference(
                                        inputs=val_data["image"].to(_device_in),
                                        roi_size=roi_size_valid,
                                        sw_batch_size=num_sw_batch_size,
                                        predictor=model,
                                        mode="gaussian",
                                        overlap=overlap_ratio,
                                        sw_device=device,
                                        device=_device_out,
                                    )
                                try:
                                    val_outputs = post_pred(val_outputs[0, ...])
                                except BaseException:
                                    val_outputs = post_pred(val_outputs[0, ...].to("cpu"))
                                finished = True

                            except RuntimeError as e:
                                if not any(x in str(e).lower() for x in ("memory", "cuda", "cudnn")):
                                    raise e
                                finished = False

                            if finished:
                                break

                        if finished:
                            val_outputs = val_outputs[None, ...]
                            value = compute_dice(
                                y_pred=val_outputs,
                                y=val_data["label"].to(val_outputs.device),
                                include_background=not softmax,
                                num_classes=output_classes,
                            ).to(device)
                            ref_value = compute_dice(
                                y_pred=val_outputs,
                                y=val_data["ref_label"].to(val_outputs.device),
                                include_background=not softmax,
                                num_classes=output_classes,
                            ).to(device)
                            hausdorff_value = compute_hausdorff_distance_95(
                                y_pred=val_outputs,
                                y=val_data["label"].to(val_outputs.device),
                                include_background=not softmax,
                                num_classes=output_classes,
                            ).to(device)
                            ref_hausdorff_value = compute_hausdorff_distance_95(
                                y_pred=val_outputs,
                                y=val_data["ref_label"].to(val_outputs.device),
                                include_background=not softmax,
                                num_classes=output_classes,
                            ).to(device)
                        else:
                            # During training, allow validation OOM for some big data to avoid crush.
                            logger.debug(f"{val_filename} is skipped due to OOM, using NaN dice values")
                            value = torch.full((1, metric_dim), float("nan")).to(device)
                            ref_value = torch.full((1, metric_dim), float("nan")).to(device)
                            hausdorff_value = torch.full((1, metric_dim), float("nan")).to(device)
                            ref_hausdorff_value = torch.full((1, metric_dim), float("nan")).to(device)
                        logger.debug(f"{_index + 1} / {len(val_loader)}/ {val_filename}: {value}, 'reference value': {ref_value}") 
                        logger.debug(f"{_index + 1} / {len(val_loader)}/ {val_filename}: {hausdorff_value}, 'reference hd value': {ref_hausdorff_value}")

                        for _c in range(metric_dim):
                            val0 = torch.nan_to_num(value[0, _c], nan=0.0)
                            val1 = 1.0 - torch.isnan(value[0, _c]).float()
                            metric[2 * _c] += val0
                            metric[2 * _c + 1] += val1
                        # Do the same for the reference label
                        for _c in range(metric_dim):
                            val0 = torch.nan_to_num(ref_value[0, _c], nan=0.0)
                            val1 = 1.0 - torch.isnan(ref_value[0, _c]).float()
                            ref_metric[2 * _c] += val0
                            ref_metric[2 * _c + 1] += val1
                        for _c in range(metric_dim):
                            val0 = torch.nan_to_num(hausdorff_value[0, _c], nan=0.0)
                            val1 = 1.0 - torch.isnan(hausdorff_value[0, 0]).float()
                            hd_metric[2 * _c] += val0 * val1
                            hd_metric[2 * _c + 1] += val1
                        for _c in range(metric_dim):
                            val0 = torch.nan_to_num(ref_hausdorff_value[0, _c], nan=0.0)
                            val1 = 1.0 - torch.isnan(ref_hausdorff_value[0, 0]).float()
                            ref_hd_metric[2 * _c] += val0 * val1   # access every even element in the tensor (starting from 0)
                            ref_hd_metric[2 * _c + 1] += val1      # access every odd element in the tensor (starting from 1)

                        _index += 1

                    if torch.cuda.device_count() > 1:
                        dist.barrier()
                        dist.all_reduce(metric, op=torch.distributed.ReduceOp.SUM)
                        dist.all_reduce(ref_metric, op=torch.distributed.ReduceOp.SUM)
                        dist.all_reduce(hd_metric, op=torch.distributed.ReduceOp.SUM)
                        dist.all_reduce(ref_hd_metric, op=torch.distributed.ReduceOp.SUM)

                    metric = metric.tolist(); hd_metric = hd_metric.tolist()
                    ref_metric = ref_metric.tolist(); ref_hd_metric = ref_hd_metric.tolist()
                    if torch.cuda.device_count() == 1 or dist.get_rank() == 0:
                        for _c in range(metric_dim):
                            logger.debug(f"Evaluation metric - class {_c + 1}: {metric[2 * _c] / metric[2 * _c + 1]} Reference metric - class {_c + 1}: {ref_metric[2 * _c] / ref_metric[2 * _c + 1]}")
                            logger.debug(f"HD Evaluation metric - class {_c + 1}: {hd_metric[2 * _c] / hd_metric[2 * _c + 1]} HD Reference metric - class {_c + 1}: {ref_hd_metric[2 * _c] / ref_hd_metric[2 * _c + 1]}")
                            try:
                                writer.add_scalar(f"val_class/acc_{class_names[_c]}", metric[2 * _c] / metric[2 * _c + 1], epoch)
                                writer.add_scalar(f"ref val_class/acc_{class_names[_c]}", ref_metric[2 * _c] / ref_metric[2 * _c + 1], epoch)
                                writer.add_scalar(f"hd val_class/acc_{class_names[_c]}", hd_metric[2 * _c] / hd_metric[2 * _c + 1], epoch )
                                writer.add_scalar(f"hd ref val_class/acc_{class_names[_c]}", ref_hd_metric[2 * _c] / ref_hd_metric[2 * _c + 1], epoch)
                                mlflow.log_metric(f"val_class/acc_{class_names[_c]}", metric[2 * _c] / metric[2 * _c + 1], step=epoch)
                                mlflow.log_metric(f"ref val_class/acc_{class_names[_c]}", ref_metric[2 * _c] / ref_metric[2 * _c + 1], step=epoch)
                                mlflow.log_metric(f"hd val_class/acc_{class_names[_c]}", hd_metric[2 * _c] / hd_metric[2 * _c + 1], step=epoch)
                                mlflow.log_metric(f"hd ref val_class/acc_{class_names[_c]}", ref_hd_metric[2 * _c] / ref_hd_metric[2 * _c + 1], step=epoch)
                            except BaseException:
                                writer.add_scalar(f"val_class/acc_{_c}", metric[2 * _c] / metric[2 * _c + 1], epoch)
                                writer.add_scalar(f"ref val_class/acc_{_c}", ref_metric[2 * _c] / ref_metric[2 * _c + 1], epoch)
                                writer.add_scalar(f"hd val_class/acc_{_c}", hd_metric[2 * _c] / hd_metric[2 * _c + 1], epoch)
                                writer.add_scalar(f"hd ref val_class/acc_{_c}", ref_hd_metric[2 * _c] / ref_hd_metric[2 * _c + 1], epoch)
                                mlflow.log_metric(f"val_class/acc_{_c}", metric[2 * _c] / metric[2 * _c + 1], step=epoch)
                                mlflow.log_metric(f"ref val_class/acc_{_c}", ref_metric[2 * _c] / ref_metric[2 * _c + 1], step=epoch)
                                mlflow.log_metric(f"hd val_class/acc_{_c}", hd_metric[2 * _c] / hd_metric[2 * _c + 1], step=epoch)
                                mlflow.log_metric(f"hd ref val_class/acc_{_c}", ref_hd_metric[2 * _c] / ref_hd_metric[2 * _c + 1], step=epoch)

                        avg_metric = 0; avg_hd_metric = 0
                        avg_metric_ref = 0; avg_hd_metric_ref = 0
                        for _c in range(metric_dim):
                            avg_metric += metric[2 * _c] / metric[2 * _c + 1]
                            avg_metric_ref += ref_metric[2 * _c] / ref_metric[2 * _c + 1]
                            avg_hd_metric += hd_metric[2 * _c] / hd_metric[2 * _c + 1]
                            avg_hd_metric_ref += ref_hd_metric[2 * _c] / ref_hd_metric[2 * _c + 1]
                        avg_metric = avg_metric / float(metric_dim)
                        avg_metric_ref = avg_metric_ref / float(metric_dim)
                        avg_hd_metric = avg_hd_metric / float(metric_dim)
                        avg_hd_metric_ref = avg_hd_metric_ref / float(metric_dim)
                        logger.debug(f"Avg_metric: {avg_metric} Avg_metric_ref: {avg_metric_ref}")
                        logger.debug(f"Avg_hd_metric: {avg_hd_metric} Avg_hd_metric_ref: {avg_hd_metric_ref}")
                        
                        writer.add_scalar("val/acc", avg_metric, epoch)
                        mlflow.log_metric("val/acc", avg_metric, step=epoch)
                        writer.add_scalar("val/acc_ref", avg_metric_ref, epoch)
                        mlflow.log_metric("val/acc_ref", avg_metric_ref, step=epoch)
                        writer.add_scalar("hd val/acc", avg_hd_metric, epoch)
                        mlflow.log_metric("hd val/acc", avg_hd_metric, step=epoch)
                        writer.add_scalar("hd val/acc_ref", avg_hd_metric_ref, epoch)
                        mlflow.log_metric("hd val/acc_ref", avg_hd_metric_ref, step=epoch)

                        if avg_metric > best_metric:
                            best_metric = avg_metric
                            best_metric_epoch = epoch
                            if torch.cuda.device_count() > 1:
                                torch.save(model.module.state_dict(), os.path.join(ckpt_path, "best_metric_model.pt"))
                            else:
                                torch.save(model.state_dict(), os.path.join(ckpt_path, "best_metric_model.pt"))
                            logger.debug("Saved new best metric model")

                            dict_file = {}
                            dict_file["best_avg_dice_score"] = float(best_metric)
                            dict_file["best_avg_dice_score_epoch"] = int(best_metric_epoch)
                            dict_file["best_avg_dice_score_iteration"] = int(idx_iter)
                            with open(os.path.join(ckpt_path, "progress.yaml"), "a") as out_file:
                                yaml.dump([dict_file], stream=out_file)

                        if avg_hd_metric < best_hd_metric:
                            best_hd_metric = avg_hd_metric
                            best_hd_metric_epoch = epoch + 1
                            if torch.cuda.device_count() > 1:
                                torch.save(model.module.state_dict(), os.path.join(ckpt_path, "best_hd_metric_model.pt"))
                            else:
                                torch.save(model.state_dict(), os.path.join(ckpt_path, "best_hd_metric_model.pt"))
                            logger.debug("Saved new best hausdorff metric model")

                            dict_file = {}
                            dict_file["best_avg_hd_score"] = float(best_hd_metric)
                            dict_file["best_avg_hd_score_epoch"] = int(best_hd_metric_epoch)
                            dict_file["best_avg_hd_score_iteration"] = int(idx_iter)
                            with open(os.path.join(ckpt_path, "hd_progress.yaml"), "a") as out_file:
                                yaml.dump([dict_file], stream=out_file)

                        logger.debug(
                            "Current epoch: {} current mean dice: {:.4f}, current reference mean dice: {:.4f},  best mean dice: {:.4f} at epoch {}".format(
                                epoch + 1, avg_metric, avg_metric_ref, best_metric, best_metric_epoch
                            )
                        )
                        logger.debug(
                            "Current epoch: {} current mean hausdorff: {:.4f}, current reference mean hausdorff: {:.4f},  best mean hausdorff: {:.4f} at epoch {}".format(
                                epoch + 1, avg_hd_metric, avg_hd_metric_ref, best_hd_metric, best_hd_metric_epoch
                            )
                        )

                        current_time = time.time()
                        elapsed_time = (current_time - start_time) / 60.0
                        with open(os.path.join(ckpt_path, "accuracy_history.csv"), "a") as f:
                            f.write(
                                "{:d}\t{:.5f}\t{:.5f}\t{:.5f}\t{:.5f}\t{:.5f}\t{:.5f}\t{:.5f}\t{:.1f}\t{:d}\n".format(
                                epoch + 1, avg_metric, avg_metric_ref, avg_hd_metric, avg_hd_metric_ref, loss_torch_epoch, loss_torch_epoch_t1xflair, lr, elapsed_time, idx_iter
                                )
                            )

                        if es:
                            early_stopping(val_acc=avg_metric)
                            stop_train = torch.tensor(early_stopping.early_stop).to(device)

                    if torch.cuda.device_count() > 1:
                        dist.barrier()

                    if es:
                        if torch.cuda.device_count() > 1:
                            dist.broadcast(stop_train, src=0)
                        if stop_train:
                            break

                torch.cuda.empty_cache()

        if valid_at_orig_resolution_at_last or valid_at_orig_resolution_only:
            if torch.cuda.device_count() == 1 or dist.get_rank() == 0:
                logger.debug(f"{os.path.basename(bundle_root)} - validation at original resolution")
                logger.debug("Validation at original resolution")

            if torch.cuda.device_count() > 1:
                model.module.load_state_dict(
                    torch.load(os.path.join(ckpt_path, "best_metric_model.pt"), map_location=device)
                )
            else:
                model.load_state_dict(torch.load(os.path.join(ckpt_path, "best_metric_model.pt"), map_location=device))
            logger.debug("Checkpoints loaded")

            model.eval()
            with torch.no_grad():
                metric = torch.zeros(metric_dim * 2, dtype=torch.float, device=device)
                ref_metric = torch.zeros(metric_dim * 2, dtype=torch.float, device=device)
                hd_metric = torch.zeros(metric_dim * 2, dtype=torch.float, device=device)
                ref_hd_metric = torch.zeros(metric_dim * 2, dtype=torch.float, device=device)

                _index = 0
                for val_data in orig_val_loader:
                    try:
                        val_filename = val_data["image_meta_dict"]["filename_or_obj"][0]
                    except BaseException:
                        val_filename = val_data["image"].meta["filename_or_obj"][0]
                    if sw_input_on_cpu:
                        device_list_input = ["cpu"]
                        device_list_output = ["cpu"]
                    else:
                        device_list_input = [device, device, "cpu"]
                        device_list_output = [device, "cpu", "cpu"]

                    for _device_in, _device_out in zip(device_list_input, device_list_output):
                        try:
                            val_data["pred"] = None
                            with autocast(enabled=amp):
                                val_data["pred"] = sliding_window_inference(
                                    inputs=val_data["image"].to(_device_in),
                                    roi_size=roi_size_valid,
                                    sw_batch_size=num_sw_batch_size,
                                    predictor=model,
                                    mode="gaussian",
                                    overlap=overlap_ratio_final,
                                    sw_device=device,
                                    device=_device_out,
                                )

                            finished = True
                        except RuntimeError as e:
                            if not any(x in str(e).lower() for x in ("memory", "cuda", "cudnn")):
                                raise e
                            finished = False
                            torch.cuda.empty_cache()
                        if finished:
                            break

                    if finished:
                        # move all to cpu to avoid potential out memory in invert transform
                        val_data["pred"] = val_data["pred"].to("cpu")
                        val_data["image"] = val_data["image"].to("cpu")
                        val_data["label"] = val_data["label"].to("cpu")
                        val_data["ref_label"] = val_data["ref_label"].to("cpu")
                        torch.cuda.empty_cache()
                        val_data = [post_transforms(i) for i in monai.data.decollate_batch(val_data)]
                        val_outputs = val_data[0]["pred"][None, ...]

                        value = compute_dice(
                            y_pred=val_outputs,
                            y=val_data[0]["label"][None, ...].to(val_outputs.device),
                            include_background=not softmax,
                            num_classes=output_classes,
                        ).to(device)
                        ref_value = compute_dice(
                            y_pred=val_outputs,
                            y=val_data[0]["ref_label"][None, ...].to(val_outputs.device),
                            include_background=not softmax,
                            num_classes=output_classes,
                        ).to(device)
                        hausdorff_value = compute_hausdorff_distance_95(
                            y_pred=val_outputs,
                            y=val_data[0]["label"][None, ...].to(val_outputs.device),
                            include_background=not softmax,
                            num_classes=output_classes,
                        ).to(device)
                        ref_hausdorff_value = compute_hausdorff_distance_95(
                            y_pred=val_outputs,
                            y=val_data[0]["ref_label"][None, ...].to(val_outputs.device),
                            include_background=not softmax,
                            num_classes=output_classes,
                        ).to(device)
                    else:
                        logger.debug(f"{val_filename} is skipped due to OOM, using NaN dice values")
                        value = torch.full((1, metric_dim), float("nan")).to(device)
                        ref_value = torch.full((1, metric_dim), float("nan")).to(device)
                        hausdorff_value = torch.full((1, metric_dim), float("nan")).to(device)
                        ref_hausdorff_value = torch.full((1, metric_dim), float("nan")).to(device)

                    logger.debug(
                        f"Validation Dice score at original resolution: {_index + 1} / {len(orig_val_loader)}/ {val_filename}: {value}" + f"Reference Dice score at original resolution: {_index + 1} / {len(orig_val_loader)}/ {val_filename}: {ref_value}"
                    )
                    logger.debug(
                        f"Validation HD score at original resolution: {_index + 1} / {len(orig_val_loader)}/ {val_filename}: {hausdorff_value}" + f"Reference HD score at original resolution: {_index + 1} / {len(orig_val_loader)}/ {val_filename}: {ref_hausdorff_value}"
                    )


                    for _c in range(metric_dim):
                        val0 = torch.nan_to_num(value[0, _c], nan=0.0)
                        val1 = 1.0 - torch.isnan(value[0, _c]).float()
                        metric[2 * _c] += val0
                        metric[2 * _c + 1] += val1
                    
                    # Do the same for the reference label
                    for _c in range(metric_dim):
                        val0 = torch.nan_to_num(ref_value[0, _c], nan=0.0)
                        val1 = 1.0 - torch.isnan(ref_value[0, _c]).float()
                        ref_metric[2 * _c] += val0
                        ref_metric[2 * _c + 1] += val1
                    for _c in range(metric_dim):
                        val0 = torch.nan_to_num(hausdorff_value[0, _c], nan=0.0)
                        val1 = 1.0 - torch.isnan(hausdorff_value[0, 0]).float()
                        hd_metric[2 * _c] += val0 * val1
                        hd_metric[2 * _c + 1] += val1
                    for _c in range(metric_dim):
                        val0 = torch.nan_to_num(ref_hausdorff_value[0, _c], nan=0.0)
                        val1 = 1.0 - torch.isnan(ref_hausdorff_value[0, 0]).float()
                        ref_hd_metric[2 * _c] += val0 * val1
                        ref_hd_metric[2 * _c + 1] += val1

                    _index += 1

                if torch.cuda.device_count() > 1:
                    dist.barrier()
                    dist.all_reduce(metric, op=torch.distributed.ReduceOp.SUM)
                    dist.all_reduce(ref_metric, op=torch.distributed.ReduceOp.SUM)
                    dist.all_reduce(hd_metric, op=torch.distributed.ReduceOp.SUM)
                    dist.all_reduce(ref_hd_metric, op=torch.distributed.ReduceOp.SUM)

                metric = metric.tolist()
                if torch.cuda.device_count() == 1 or dist.get_rank() == 0:
                    for _c in range(metric_dim):
                        logger.debug(
                            f"Evaluation metric at original resolution - class {_c + 1}: {metric[2 * _c] / metric[2 * _c + 1]}"
                        )
                        logger.debug(
                            f"HD Evaluation metric at original resolution - class {_c + 1}: {hd_metric[2 * _c] / hd_metric[2 * _c + 1]}"
                        )

                    avg_metric = 0; avg_hd_metric = 0
                    avg_metric_ref = 0; avg_hd_metric_ref = 0
                    for _c in range(metric_dim):
                        avg_metric += metric[2 * _c] / metric[2 * _c + 1]
                        avg_metric_ref += ref_metric[2 * _c] / ref_metric[2 * _c + 1]
                        avg_hd_metric += hd_metric[2 * _c] / hd_metric[2 * _c + 1]
                        avg_hd_metric_ref += ref_hd_metric[2 * _c] / ref_hd_metric[2 * _c + 1]
                    avg_metric = avg_metric / float(metric_dim)
                    avg_metric_ref = avg_metric_ref / float(metric_dim)
                    avg_hd_metric = avg_hd_metric / float(metric_dim)
                    avg_hd_metric_ref = avg_hd_metric_ref / float(metric_dim)

                    logger.debug(f"Avg_metric at original resolution: {avg_metric}" + f"Avg_metric_ref at original resolution: {avg_metric_ref}")
                    logger.debug(f"Avg_hd_metric at original resolution: {avg_hd_metric}" + f"Avg_hd_metric_ref at original resolution: {avg_hd_metric_ref}")
                    writer.add_scalar("val/acc_orig_res", avg_metric, epoch)
                    mlflow.log_metric("val/acc_orig_res", avg_metric, step=epoch)
                    writer.add_scalar("val/acc_ref_orig_res", avg_metric_ref, epoch)
                    mlflow.log_metric("val/acc_ref_orig_res", avg_metric_ref, step=epoch)
                    writer.add_scalar("hd val/acc_orig_res", avg_hd_metric, epoch)
                    mlflow.log_metric("hd val/acc_orig_res", avg_hd_metric, step=epoch)
                    writer.add_scalar("hd val/acc_ref_orig_res", avg_hd_metric_ref, epoch)
                    mlflow.log_metric("hd val/acc_ref_orig_res", avg_hd_metric_ref, step=epoch)

                    with open(os.path.join(ckpt_path, "progress.yaml"), "r") as out_file:
                        progress = yaml.safe_load(out_file)

                    dict_file = {}
                    dict_file["best_avg_dice_score"] = float(avg_metric)
                    dict_file["best_avg_dice_score_epoch"] = int(progress[-1]["best_avg_dice_score_epoch"])
                    dict_file["best_avg_dice_score_iteration"] = int(progress[-1]["best_avg_dice_score_iteration"])
                    dict_file["inverted_best_validation"] = True

                    with open(os.path.join(ckpt_path, "progress.yaml"), "a") as out_file:
                        yaml.dump([dict_file], stream=out_file)


                if torch.cuda.device_count() > 1:
                    dist.barrier()

    if torch.cuda.device_count() == 1 or dist.get_rank() == 0:
        logger.debug(f"Training completed, best_metric: {best_metric:.4f} at epoch: {best_metric_epoch}.")

        writer.flush()
        writer.close()
        mlflow.end_run()

    if torch.cuda.device_count() == 1 or dist.get_rank() == 0:
        if es and not valid_at_orig_resolution_only and (_round + 1) < num_rounds:
            logger.warning(f"{os.path.basename(bundle_root)} - training: finished with early stop")
        else:
            logger.warning(f"{os.path.basename(bundle_root)} - training: finished")

    if torch.cuda.device_count() > 1:
        dist.destroy_process_group()

    return


if __name__ == "__main__":
    from monai.utils import optional_import

    fire, _ = optional_import("fire")
    fire.Fire()