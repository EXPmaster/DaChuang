import torch.nn as nn
import torch
from .general import bbox_iou
from .postprocess import build_targets


class MultiHeadLoss(nn.Module):
    """
    collect all the loss we need
    """
    def __init__(self, losses, cfg, lambdas=None):
        """
        Inputs:
        - losses: (list)[nn.Module, nn.Module, ...]
        - cfg: config object
        - lambdas: (list) + IoU loss, 各个loss的权重
        """
        super().__init__()
        # lambdas: [cls, obj, iou, seg]
        if not lambdas:
            lambdas = [1.0 for _ in range(len(losses) + 1)]
        assert all(lam >= 0.0 for lam in lambdas)

        self.losses = nn.ModuleList(losses)
        self.lambdas = lambdas
        self.cfg = cfg

    def forward(self, head_fields, head_targets, model):
        """
        Inputs:
        - head_fields: (list)各个head输出的数据
        - head_targets: (list)各个head对应的gt
        - model:

        Returns:
        - total_loss: sum of all the loss
        - head_losses: (tuple) 包含所有loss[loss1, loss2, ...]

        More:
        我感觉head_losses 也可以做成字典, 比如'classification':loss1
        看哪个比较方便
        """
        # head_losses = [ll
        #                 for l, f, t in zip(self.losses, head_fields, head_targets)
        #                 for ll in l(f, t)]
        #
        # assert len(self.lambdas) == len(head_losses)
        # loss_values = [lam * l
        #                for lam, l in zip(self.lambdas, head_losses)
        #                if l is not None]
        # total_loss = sum(loss_values) if loss_values else None
        total_loss, head_losses = self._forward_impl(head_fields, head_targets, model)

        return total_loss, head_losses

    def _forward_impl(self, predictions, targets, model):
        """

        Args:
            predictions: predicts of [det_head1, det_head2, det_head3, seg_head]
            targets: gts [det_targets, segment_targets]
            model:

        Returns:
            total_loss: sum of all the loss
            head_losses: list containing losses

        """
        cfg = self.cfg
        device = targets[0].device
        lcls, lbox, lobj = torch.zeros(1, device=device), torch.zeros(1, device=device), torch.zeros(1, device=device)
        tcls, tbox, indices, anchors = build_targets(cfg, predictions[0], targets[0], model)  # targets

        # Class label smoothing https://arxiv.org/pdf/1902.04103.pdf eqn 3
        cp, cn = smooth_BCE(eps=0.0)

        BCEcls, BCEobj, BCEseg = self.losses

        # Calculate Losses
        nt = 0  # number of targets
        no = len(predictions[0])  # number of outputs
        balance = [4.0, 1.0, 0.4] if no == 3 else [4.0, 1.0, 0.4, 0.1]  # P3-5 or P3-6

        # calculate detection loss
        for i, pi in enumerate(predictions[0]):  # layer index, layer predictions
            b, a, gj, gi = indices[i]  # image, anchor, gridy, gridx
            tobj = torch.zeros_like(pi[..., 0], device=device)  # target obj

            n = b.shape[0]  # number of targets
            if n:
                nt += n  # cumulative targets
                ps = pi[b, a, gj, gi]  # prediction subset corresponding to targets

                # Regression
                pxy = ps[:, :2].sigmoid() * 2. - 0.5
                pwh = (ps[:, 2:4].sigmoid() * 2) ** 2 * anchors[i]
                pbox = torch.cat((pxy, pwh), 1).to(device)  # predicted box
                iou = bbox_iou(pbox.T, tbox[i], x1y1x2y2=False, CIoU=True)  # iou(prediction, target)
                lbox += (1.0 - iou).mean()  # iou loss

                # Objectness
                tobj[b, a, gj, gi] = (1.0 - model.gr) + model.gr * iou.detach().clamp(0).type(tobj.dtype)  # iou ratio

                # Classification
                if model.nc > 1:  # cls loss (only if multiple classes)
                    t = torch.full_like(ps[:, 5:], cn, device=device)  # targets
                    t[range(n), tcls[i]] = cp
                    lcls += BCEcls(ps[:, 5:], t)  # BCE

            lobj += BCEobj(pi[..., 4], tobj) * balance[i]  # obj loss

        seg_predicts = predictions[-1].view(-1)
        seg_targets = targets[-1].view(-1)
        lseg = BCEseg(seg_predicts, seg_targets)

        s = 3 / no  # output count scaling
        lcls *= cfg.LOSS.CLS_GAIN * s * self.lambdas[0]
        lobj *= cfg.LOSS.OBJ_GAIN * s * (1.4 if no == 4 else 1.) * self.lambdas[1]
        lbox *= cfg.LOSS.BOX_GAIN * s * self.lambdas[2]
        lseg *= cfg.LOSS.SEG_GAIN * self.lambdas[3]

        # bs = tobj.shape[0]  # batch size
        if cfg.TRAIN.FREEZE_SEG:
            lseg = 0 * lseg
        loss = lbox + lobj + lcls + lseg
        # return loss * bs, torch.cat((lbox, lobj, lcls, loss)).detach()
        return loss, (lbox.item(), lobj.item(), lcls.item(), lseg.item(), loss.item())


def get_loss(cfg, device):
    """
    get MultiHeadLoss

    Inputs:
    -cfg: configuration use the loss_name part or 
          function part(like regression classification)
    -device: cpu or gpu device

    Returns:
    -loss: (MultiHeadLoss)

    More:
    通过loss的种类调用各种loss,组成一个list
    用这个list调用MultiHeadLoss类
    """
    # class loss criteria
    BCEcls = nn.BCEWithLogitsLoss(pos_weight=torch.Tensor([cfg.LOSS.CLS_POS_WEIGHT])).to(device)
    # object loss criteria
    BCEobj = nn.BCEWithLogitsLoss(pos_weight=torch.Tensor([cfg.LOSS.OBJ_POS_WEIGHT])).to(device)
    # segmentation loss criteria
    BCEseg = nn.BCEWithLogitsLoss(pos_weight=torch.Tensor([cfg.LOSS.SEG_POS_WEIGHT])).to(device)
    # Focal loss
    gamma = cfg.LOSS.FL_GAMMA  # focal loss gamma
    if gamma > 0:
        BCEcls, BCEobj = FocalLoss(BCEcls, gamma), FocalLoss(BCEobj, gamma)

    loss_list = [BCEcls, BCEobj, BCEseg]
    loss = MultiHeadLoss(loss_list, cfg=cfg, lambdas=cfg.LOSS.MULTI_HEAD_LAMBDA)
    return loss

# example
# class L1_Loss(nn.Module)


def smooth_BCE(eps=0.1):  # https://github.com/ultralytics/yolov3/issues/238#issuecomment-598028441
    # return positive, negative label smoothing BCE targets
    return 1.0 - 0.5 * eps, 0.5 * eps


class FocalLoss(nn.Module):
    # Wraps focal loss around existing loss_fcn(), i.e. criteria = FocalLoss(nn.BCEWithLogitsLoss(), gamma=1.5)
    def __init__(self, loss_fcn, gamma=1.5, alpha=0.25):
        # alpha 正负样本平衡
        # gamma 困难样本挖掘
        super(FocalLoss, self).__init__()
        self.loss_fcn = loss_fcn  # must be nn.BCEWithLogitsLoss()
        self.gamma = gamma
        self.alpha = alpha
        self.reduction = loss_fcn.reduction
        self.loss_fcn.reduction = 'none'  # required to apply FL to each element

    def forward(self, pred, true):
        loss = self.loss_fcn(pred, true)
        # p_t = torch.exp(-loss)
        # loss *= self.alpha * (1.000001 - p_t) ** self.gamma  # non-zero power for gradient stability

        # TF implementation https://github.com/tensorflow/addons/blob/v0.7.1/tensorflow_addons/losses/focal_loss.py
        pred_prob = torch.sigmoid(pred)  # prob from logits
        p_t = true * pred_prob + (1 - true) * (1 - pred_prob)
        alpha_factor = true * self.alpha + (1 - true) * (1 - self.alpha)
        modulating_factor = (1.0 - p_t) ** self.gamma
        loss *= alpha_factor * modulating_factor

        if self.reduction == 'mean':
            return loss.mean()
        elif self.reduction == 'sum':
            return loss.sum()
        else:  # 'none'
            return loss
