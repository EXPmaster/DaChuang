import argparse
import os, sys
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(BASE_DIR)

import pprint
import torch
import torch.nn.parallel
from torch.nn.parallel import DistributedDataParallel as DDP
import torch.distributed as dist
import torch.backends.cudnn as cudnn
import torch.optim
import torch.utils.data
import torch.utils.data.distributed
import torchvision.transforms as transforms
import numpy as np
from tensorboardX import SummaryWriter

import lib.dataset as dataset
from lib.config import cfg
from lib.config import update_config
from lib.core.loss import get_loss
from lib.core.function import train
from lib.core.function import validate
from lib.core.general import fitness
from lib.models import get_net
from lib.utils import is_parallel
from lib.utils.utils import get_optimizer
from lib.utils.utils import save_checkpoint
from lib.utils.utils import create_logger, select_device
from lib.utils.autoanchor import check_anchors



def parse_args():
    parser = argparse.ArgumentParser(description='Train Multitask network')
    # general
    # parser.add_argument('--cfg',
    #                     help='experiment configure file name',
    #                     required=True,
    #                     type=str)

    # philly
    parser.add_argument('--modelDir',
                        help='model directory',
                        type=str,
                        default='')
    parser.add_argument('--logDir',
                        help='log directory',
                        type=str,
                        default='log/')
    parser.add_argument('--dataDir',
                        help='data directory',
                        type=str,
                        default='')
    parser.add_argument('--prevModelDir',
                        help='prev Model directory',
                        type=str,
                        default='')

    parser.add_argument('--sync-bn', action='store_true', help='use SyncBatchNorm, only available in DDP mode')
    parser.add_argument('--local_rank', type=int, default=-1, help='DDP parameter, do not modify')

    args = parser.parse_args()

    return args


def main():
    # set all the configurations
    args = parse_args()
    update_config(cfg, args)

    # Set DDP variables
    world_size = int(os.environ['WORLD_SIZE']) if 'WORLD_SIZE' in os.environ else 1
    global_rank = int(os.environ['RANK']) if 'RANK' in os.environ else -1

    rank = global_rank
    # TODO: handle distributed training logger
    # set the logger, tb_log_dir means tensorboard logdir

    logger, final_output_dir, tb_log_dir = create_logger(
        cfg, args.logDir, 'train', rank=rank)

    if rank in [-1, 0]:
        logger.info(pprint.pformat(args))
        logger.info(cfg)

        writer_dict = {
            'writer': SummaryWriter(log_dir=tb_log_dir),
            'train_global_steps': 0,
            'valid_global_steps': 0,
        }
    else:
        writer_dict = None

    # cudnn related setting
    cudnn.benchmark = cfg.CUDNN.BENCHMARK
    torch.backends.cudnn.deterministic = cfg.CUDNN.DETERMINISTIC
    torch.backends.cudnn.enabled = cfg.CUDNN.ENABLED

    # bulid up model
    print("begin to bulid up model")
    model = get_net(cfg)
    
    print("DDP mode")
    # DPP mode
    device = select_device(logger, batch_size=cfg.TRAIN.BATCH_SIZE_PER_GPU) if not cfg.DEBUG \
        else select_device(logger, 'cpu')

    if args.local_rank != -1:
        assert torch.cuda.device_count() > args.local_rank
        torch.cuda.set_device(args.local_rank)
        device = torch.device('cuda', args.local_rank)
        dist.init_process_group(backend='nccl', init_method='env://')  # distributed backend

    model = get_net(cfg).to(device)

    # define loss function (criterion) and optimizer
    criterion = get_loss(cfg, device=device)
    optimizer = get_optimizer(cfg, model)

    # load checkpoint model
    best_perf = 0.0
    best_model = False
    last_epoch = -1
    lr_scheduler = torch.optim.lr_scheduler.MultiStepLR(
        optimizer, cfg.TRAIN.LR_STEP, cfg.TRAIN.LR_FACTOR,
        last_epoch=last_epoch
    )
    begin_epoch = cfg.TRAIN.BEGIN_EPOCH

    checkpoint_file = os.path.join(
        final_output_dir, 'checkpoint.pth'
    )

    if rank in [-1, 0]:
        if cfg.AUTO_RESUME and os.path.exists(checkpoint_file):
            logger.info("=> loading checkpoint '{}'".format(checkpoint_file))
            checkpoint = torch.load(checkpoint_file)
            begin_epoch = checkpoint['epoch']
            best_perf = checkpoint['perf']
            last_epoch = checkpoint['epoch']
            model.load_state_dict(checkpoint['state_dict'])

            optimizer.load_state_dict(checkpoint['optimizer'])
            logger.info("=> loaded checkpoint '{}' (epoch {})".format(
                checkpoint_file, checkpoint['epoch']))

    # DP mode
    if rank == -1 and torch.cuda.device_count() > 1:
        model = torch.nn.DataParallel(model, device_ids=cfg.GPUS).cuda()

    # # DDP mode
    if rank != -1:
        model = DDP(model, device_ids=[args.local_rank], output_device=args.local_rank)

    # assign model params
    model.gr = 1.0
    model.nc = 13

    device = select_device(logger, batch_size=cfg.TRAIN.BATCH_SIZE_PER_GPU) if not cfg.DEBUG \
        else select_device(logger, 'cpu')
    print('bulid model finished')

    print("begin to load data")
    # if args.local_rank != -1:
    #     assert torch.cuda.device_count() > opt.local_rank
    #     torch.cuda.set_device(opt.local_rank)
    #     device = torch.device('cuda', opt.local_rank)
    #     dist.init_process_group(backend='nccl', init_method='env://')  # distributed backend
    #     assert opt.batch_size % opt.world_size == 0, '--batch-size must be multiple of CUDA device count'
    #     opt.batch_size = opt.total_batch_size // opt.world_size

    # Data loading
    normalize = transforms.Normalize(
        mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]
    )
    train_dataset = eval('dataset.' + cfg.DATASET.DATASET)(
        cfg=cfg,
        is_train=True,
        inputsize=cfg.MODEL.IMAGE_SIZE,
        transform=transforms.Compose([
            transforms.ToTensor(),
            normalize,
        ])
    )
    train_sampler = torch.utils.data.distributed.DistributedSampler(train_dataset) if rank != -1 else None
    valid_dataset = eval('dataset.' + cfg.DATASET.DATASET)(
        cfg=cfg,
        is_train=False,
        inputsize=cfg.MODEL.IMAGE_SIZE,
        transform=transforms.Compose([
            transforms.ToTensor(),
            normalize,
        ])
    )
    valid_sampler = torch.utils.data.distributed.DistributedSampler(valid_dataset) if rank != -1 else None

    train_loader = torch.utils.data.DataLoader(
        train_dataset,
        batch_size=cfg.TRAIN.BATCH_SIZE_PER_GPU * len(cfg.GPUS),
        shuffle=(cfg.TRAIN.SHUFFLE & rank == -1),
        num_workers=cfg.WORKERS,
        sampler=train_sampler,
        pin_memory=cfg.PIN_MEMORY,
        collate_fn=dataset.AutoDriveDataset.collate_fn
    )

    valid_loader = torch.utils.data.DataLoader(
        valid_dataset,
        batch_size=cfg.TEST.BATCH_SIZE_PER_GPU * len(cfg.GPUS),
        shuffle=False,
        num_workers=cfg.WORKERS,
<<<<<<< HEAD
=======
        sampler=valid_sampler,
>>>>>>> dfec1d144e754c66b996cecf3826b6bac2481380
        pin_memory=cfg.PIN_MEMORY,
        collate_fn=dataset.AutoDriveDataset.collate_fn
    )
    print('load data finished')

    if rank in [-1, 0]:
        if cfg.NEED_AUTOANCHOR:
            print("begin check anchors")
            check_anchors(train_dataset, model=model, imgsz=min(cfg.MODEL.IMAGE_SIZE))
    # assign model params
    model.gr = 1.0
    model.nc = 13

    # training
    print('=> start training...')
    for epoch in range(begin_epoch+1, cfg.TRAIN.END_EPOCH+1):
        if rank != -1:
            train_loader.sampler.set_epoch(epoch)
        # train for one epoch
        print("begin "+str(epoch)+" epoch")
        train(cfg, train_loader, model, criterion, optimizer,
              epoch, writer_dict, logger, device, rank)
        
        lr_scheduler.step(epoch)

        # evaluate on validation set
        if epoch % cfg.TRAIN.VAL_FREQ == 0 or epoch == cfg.TRAIN.END_EPOCH+1 and rank in [-1, 0]:
            segment_result,results, maps, times = validate(
                cfg, valid_loader, valid_dataset, model, criterion,
                final_output_dir, tb_log_dir, writer_dict,
                logger, device, rank
            )
            fi = fitness(np.array(results).reshape(1, -1))  #目标检测评价指标
            
            # TODO: validation
            # if perf_indicator >= best_perf:
            #     best_perf = perf_indicator
            #     best_model = True
            # else:
            #     best_model = False

        # save checkpoint model and best model
        if rank in [-1, 0]:
            savepath = os.path.join(final_output_dir, f'epoch-{epoch}.pth')
            logger.info('=> saving checkpoint to {}'.format(savepath))
            save_checkpoint(
                epoch=epoch,
                name=cfg.MODEL.NAME,
                model=model,
                # 'best_state_dict': model.module.state_dict(),
                # 'perf': perf_indicator,
                optimizer=optimizer,
                output_dir=final_output_dir,
                filename=f'epoch-{epoch}.pth'
            )

    # save final model
    if rank in [-1, 0]:
        final_model_state_file = os.path.join(
            final_output_dir, 'final_state.pth'
        )
        logger.info('=> saving final model state to {}'.format(
            final_model_state_file)
        )
        model_state = model.module.state_dict() if is_parallel(model) else model.state_dict()
        torch.save(model_state, final_model_state_file)
        writer_dict['writer'].close()
    else:
        dist.destroy_process_group()


if __name__ == '__main__':
    main()