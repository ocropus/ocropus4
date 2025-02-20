import io, json, sys
from io import StringIO
from typing import Any, Dict, List, Optional
from click.core import Option
import gc
import pickle
import matplotlib.pyplot as plt
import psutil
import numpy as np
import PIL
import PIL.Image
import pytorch_lightning as pl
import torch
from torch.nn.modules.module import register_module_backward_hook
import yaml
import typer
from matplotlib import gridspec
from pytorch_lightning.callbacks import LearningRateMonitor
from pytorch_lightning.callbacks.model_checkpoint import ModelCheckpoint
from scipy import ndimage as ndi
from torch import nn
from torch.optim.lr_scheduler import LambdaLR, ExponentialLR
import torchvision
import PIL


from . import confparse, segmodels, segdata, utils

app = typer.Typer()


def bit_reverse_table():
    result = torch.zeros((256,), dtype=torch.uint8)
    for i in range(256):
        result[i] = int("{:08b}".format(i)[::-1], 2)
    return result


def log_mem(logger, step):
    t = torch.cuda.get_device_properties(0).total_memory
    r = torch.cuda.memory_reserved(0)
    a = torch.cuda.memory_allocated(0)
    logger.add_scalars("gpumem", dict(total=t, reserved=r, allocated=a), step)
    m = psutil.virtual_memory()
    logger.add_scalars("cpumem", dict(used=m.used, free=m.free, active=m.active), step)


def pil_image_grid(images: list, rows: int, columns: int, title: str = None):
    """
    Create a grid of images from a list of PIL images.
    """

    # compute total width and height
    width = max(image.size[0] for image in images)
    height = max(image.size[1] for image in images)

    # create a new image with the correct size
    grid = PIL.Image.new(mode="RGB", size=(width * columns, height * rows), color=(255, 255, 255))

    # paste each image into the grid
    for row in range(rows):
        for column in range(columns):
            image = images[row * columns + column]
            grid.paste(image, (column * width, row * height))

    # return the grid
    return grid


class SegLightning(pl.LightningModule):
    def __init__(
        self,
        *,
        mname="seg",
        margin=16,
        lr=0.01,
        # lr_halflife=10,
        lr_scale=1e-3,
        lr_steps=100,
        display_freq=100,
        segmodel: Dict[Any, Any] = {},
        noutput: int = 4,
    ):
        super().__init__()
        self.save_hyperparameters()
        segmodel.setdefault("config", {})["noutput"] = noutput
        self.model = segmodels.SegModel(mname, **segmodel)
        self.get_jit_model()
        print("model created and is JIT-able")

    def get_jit_model(self):
        script = torch.jit.script(self.model)
        return script

    def configure_optimizers(self):
        optimizer = torch.optim.SGD(self.model.parameters(), lr=self.hparams.lr)
        print(f"# optimizer {optimizer}")
        scheduler = LambdaLR(optimizer, self.schedule)
        print(f"# scheduler {scheduler}")
        return [optimizer], [scheduler]

    def schedule(self, epoch: int):
        gamma = self.hparams.lr_scale ** (1.0 / self.hparams.lr_steps)
        return gamma ** min(epoch, self.hparams.lr_steps)

    def compute_loss(self, outputs, targets, mask=None):
        """Compute loss taking a margin into account."""
        b, d, h, w = outputs.shape
        b1, h1, w1 = targets.shape
        assert h <= h1 and w <= w1 and h1 - h < 5 and w1 - w < 5, (
            outputs.shape,
            targets.shape,
        )
        targets = targets[:, :h, :w]
        # lsm = outputs.log_softmax(1)
        if self.hparams.margin > 0:
            m = self.hparams.margin
            outputs = outputs[:, :, m:-m, m:-m]
            targets = targets[:, m:-m, m:-m]
            if mask is not None:
                mask = mask[:, m:-m, m:-m]
        if mask is None:
            loss = nn.CrossEntropyLoss()(outputs, targets.to(outputs.device))
        else:
            loss = nn.CrossEntropyLoss(reduction="none")(outputs, targets.to(outputs.device))
            loss = torch.sum(loss * mask.to(loss.device)) / (0.1 + mask.sum())
        return loss

    def training_step(self, batch, index, mode="train"):
        inputs, targets, mask = batch
        assert inputs.ndim == 4, inputs.shape
        assert inputs.shape[1] == 3, inputs.shape
        outputs = self.model.forward(inputs)
        assert outputs.ndim == 4, (inputs.shape, outputs.shape, targets.shape)
        assert targets.ndim == 3, (inputs.shape, outputs.shape, targets.shape)
        assert outputs.shape[0] < 100 and outputs.shape[1] < 10, outputs.shape
        assert targets.min() >= 0 and targets.max() < self.hparams.noutput
        if outputs.shape != inputs.shape:
            assert outputs.shape[0] == inputs.shape[0]
            assert outputs.ndim == 4
            bs, h, w = targets.shape
            outputs = outputs[:, :, :h, :w]
        self.last_mask = mask
        assert inputs.size(0) == outputs.size(0)
        loss = self.compute_loss(outputs, targets, mask=mask)
        self.log(f"{mode}_loss", loss)
        with torch.no_grad():
            pred = outputs.softmax(1).argmax(1)
            errors = (pred != targets).sum()
            err = float(errors) / float(targets.nelement())
            self.log(f"{mode}_err", err, prog_bar=True)
        if mode == "train" and index % self.hparams.display_freq == 0:
            print("displaying")
            self.display_result(index, inputs, targets, outputs, mask)
        return loss

    def validation_step(self, batch, index):
        return self.training_step(batch, index, mode="val")

    colors = torch.tensor([
        [0, 0, 0],
        [255, 0, 0],
        [0, 255, 0],
        [0, 0, 255],
        [255, 255, 0],
        [255, 0, 255],
        [0, 255, 255],
        [255, 255, 255],
    ], dtype=torch.uint8)

    def display_result(self, index, inputs, targets, outputs, mask, key="segmentation"):
        inputs, targets, outputs = inputs[0], targets[0], outputs[0]
        outputs = outputs.softmax(0)
        assert inputs.ndim == 3 and inputs.shape[0] == 3
        assert outputs.ndim == 3 and outputs.shape[0] >= 3
        inputs = (inputs.clip(0, 1) * 255.0).type(torch.uint8)
        outputs = (outputs.clip(0, 1) * 255.0).type(torch.uint8)
        pred = outputs.argmax(0)
        pred_rgb = self.colors[pred].permute(2, 0, 1)
        assert pred_rgb.ndim == 3 and pred_rgb.shape[0] == 3, pred_rgb.shape
        il = [inputs, pred_rgb]
        il = [torchvision.transforms.ToPILImage()(im).convert("RGB") for im in il]
        grid = pil_image_grid(il, 1, len(il), str(index))
        grid = torchvision.transforms.ToTensor()(grid)
        exp = self.logger.experiment
        if hasattr(exp, "add_image"):
            exp.add_image(key, grid, index)
        else:
            import wandb

            exp.log({key: [wandb.Image(grid, caption=key)]})

    def OLD_display_result(self, index, inputs, targets, outputs, mask):
        # better display, but leaking memory

        cmap = plt.cm.nipy_spectral

        fig = plt.figure(figsize=(10, 10))
        gs = gridspec.GridSpec(2, 2)
        fig_img, fig_out, fig_slice, fig_gt = [fig.add_subplot(gs[k // 2, k % 2]) for k in range(4)]
        fig_img.set_title(f"{index}")
        doc = inputs[0, 0].detach().cpu().numpy()
        mask = getattr(self, "last_mask")
        if mask is not None:
            mask = mask[0].cpu().detach().numpy()
            combined = np.array([doc, doc, mask]).transpose(1, 2, 0)
            fig_img.imshow(combined)
        else:
            fig_img.imshow(doc, cmap="gray")
        p = outputs.detach().cpu().softmax(1)
        assert not torch.isnan(inputs).any()
        assert not torch.isnan(outputs).any()
        b, d, h, w = outputs.size()
        result = p.numpy()[0].transpose(1, 2, 0)
        if result.shape[2] > 3:
            result = result[..., 1:4]
        else:
            result = result[..., :3]
        fig_out.imshow(result, vmin=0, vmax=1)
        # m = result.shape[1] // 2
        m = min(max(10, result[:, :, 2:].sum(2).sum(0).argmax()), result.shape[1] - 10)
        fig_out.set_title(f"x={m}")
        fig_out.plot([m, m], [0, h], color="white", alpha=0.5)
        colors = [cmap(x) for x in np.linspace(0, 1, p.shape[1])]
        for i in range(0, d):
            fig_slice.plot(p[0, i, :, m], color=colors[i % len(colors)])
        if p.shape[1] <= 4:
            t = targets[0].detach().cpu().numpy()
            t = np.array([t == 1, t == 2, t == 3]).astype(float).transpose(1, 2, 0)
            fig_gt.imshow(t)
        else:
            fig_gt.imshow(p.argmax(1)[0], vmin=0, vmax=p.shape[1], cmap=cmap)
        self.log_matplotlib_figure(fig, self.global_step, size=(1000, 1000))
        fig.clear()
        plt.close(fig)
        gc.collect()

    def OLD_log_matplotlib_figure(self, fig, index, key="image", size=(600, 600)):
        """Log the given matplotlib figure to tensorboard logger tb."""
        buf = io.BytesIO()
        fig.savefig(buf, format="jpeg")
        buf.seek(0)
        image = PIL.Image.open(buf)
        image = image.convert("RGB")
        image = image.resize(size)
        image = np.array(image)
        image = torch.from_numpy(image).float() / 255.0
        image = image.permute(2, 0, 1)
        exp = self.logger.experiment
        if hasattr(exp, "add_image"):
            exp.add_image(key, image, index)
        else:
            import wandb

            exp.log({key: [wandb.Image(image, caption=key)]})
        del buf
        del image


@app.command()
def train(
    train_bs: int = -1,
    val_bs: int = -1,
    kind: str = "words",
    augmentation: str = "default",
    num_workers: int = 8,
    nepoch: int = 200000,
    checkpoint: int = 1,
    # mname: str = "ocropus.segmodels.segmentation_model_210910",
    mname: str = "ocropus.segmodels.segmentation_model_220113",
    lr: float = 0.01,
    lr_steps: int = 100,
    lr_scale: float = 1e-3,
    # lr_halflife: int = 500000,
    display_freq: int = 50,
    max_epochs: int = 10000,
    gpus: str = "0,",
    default_root_dir: str = "./_logs",
    dumpjit: Optional[str] = None,
    maxsize: float = 8e6,
    wandb: str = "",
    traced: bool = False,
    masked: Optional[bool] = None,
    resume: Optional[str] = None,
    restart: Optional[str] = None,
) -> None:
    """Train segmentation model.

    NB: trailing / in train_shards indicates bucket to be expanded. Only works for S3-like http.
    """

    assert kind in ["words", "page"]
    if kind == "words":
        Loader = segdata.WordSegDataLoader
        train_bs = train_bs if train_bs > 0 else 8
        val_bs = val_bs if val_bs > 0 else 8
        noutput = 4
        margin = 0  # was: margin= 16
    elif kind == "page":
        Loader = segdata.PageSegDataLoader
        train_bs = train_bs if train_bs > 0 else 1
        val_bs = val_bs if val_bs > 0 else 1
        noutput = 5
        margin = -1
    else:
        raise ValueError(f"Unknown kind: {kind}")

    data = Loader(
        train_bs=train_bs,
        val_bs=val_bs,
        augmentation=augmentation,
        num_workers=num_workers,
        nepoch=nepoch,
        maxsize=maxsize,
    )
    batch = next(iter(data.train_dataloader()))
    print(
        f"# checking training batch size {batch[0].size()} {batch[1].size()}",
    )

    if restart is not None:
        ckpt = torch.load(open(restart, "rb"), map_location="cpu")
        mname = ckpt["hyper_parameters"]["mname"]

    smodel = SegLightning(
        mname=mname,
        lr=lr,
        # lr_halflife=lr_halflife,
        lr_steps=lr_steps,
        lr_scale=lr_scale,
        display_freq=display_freq,
        noutput=noutput,
        margin=margin,
    )

    if dumpjit is not None:
        assert resume is not None, "dumpjit requires a checkpoint"
        print(f"# loading {resume}")
        ckpt = torch.load(open(resume, "rb"), map_location="cpu")
        print("# setting state dict")
        smodel.cpu()
        smodel.load_state_dict(ckpt["state_dict"])
        print(smodel)
        print("# compiling jit model")
        script = smodel.get_jit_model()
        print(f"# saving {dumpjit}")
        torch.jit.save(script, dumpjit)
        print(f"# saved model to {dumpjit}")
        sys.exit(0)

    callbacks = []

    callbacks.append(
        LearningRateMonitor(logging_interval="step"),
    )
    mcheckpoint = ModelCheckpoint(
        every_n_epochs=checkpoint,
    )
    callbacks.append(mcheckpoint)

    kw = {}
    if wandb != "":
        wconfig = eval("dict(" + wandb + ")")
        print(f"# logging to {wconfig}")
        from pytorch_lightning.loggers import WandbLogger

        kw["logger"] = WandbLogger(**wconfig)
    else:
        print("# logging locally")

    trainer = pl.Trainer(
        callbacks=callbacks,
        max_epochs=max_epochs,
        gpus=gpus,
        default_root_dir=default_root_dir,
        resume_from_checkpoint=resume,
        **kw,
    )

    if traced:
        import tracemalloc

        tracemalloc.start(20)
        before = tracemalloc.take_snapshot()

    trainer.fit(smodel, data)

    if traced:
        import tracemalloc

        after = tracemalloc.take_snapshot()
        stats = after.compare_to(before, key_type="filename")
        with open("memstats.pyd", "wb") as stream:
            pickle.dump(stats, stream)


if __name__ == "__main__":
    app()
