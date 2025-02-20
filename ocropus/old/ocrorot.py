import sys
import random as pyrand
from math import cos, exp, log, sin
from typing import List
from itertools import islice
import random
import os

import matplotlib.pyplot as plt
import numpy as np
import scipy.ndimage as ndi
import torch
import typer
from torch import nn
from torch import optim
from torch.utils.data import DataLoader
import webdataset as wds
import torch.fft

from . import slog
from . import utils
from . import loading
from .utils import public

logger = slog.NoLogger()

plt.rc("image", cmap="gray")
plt.rc("image", interpolation="nearest")


app = typer.Typer()


default_extensions = (
    "bin.png;nrm.jpg;nrm.png;image.png;framed.png;page.png;png;page.jpg;jpg;jpeg"
)


def binned(x, bins):
    return np.argmin(np.abs(np.array(bins) - x))


def unbinned(b, bins):
    return bins[b]


def get_patch(image, shape, center, m=np.eye(2), order=1):
    assert np.amin(image) >= 0 and np.amax(image) <= 1.0
    yx = np.array(center, "f")
    hw = np.array(shape, "f")
    offset = yx - np.dot(m, hw / 2.0)
    return ndi.affine_transform(
        image, m, offset=offset, output_shape=shape, order=order
    ).clip(0, 1)


def rot_samples(
    page,
    npatches=32,
    ntrials=32,
    shape=(256, 256),
    alpha=(-0.03, 0.03),
    scale=(1.0, 1.0),
    rotate=True,
):
    h, w = page.shape[:2]
    smooth = ndi.uniform_filter(page, 100)
    mask = smooth > np.percentile(smooth, 70)
    samples = []
    for _ in range(ntrials):
        if len(samples) >= npatches:
            break
        y, x = pyrand.randrange(0, h), pyrand.randrange(0, w)
        if mask[y, x]:
            samples.append((x, y))
    pyrand.shuffle(samples)
    for x, y in samples:
        a = pyrand.uniform(*alpha)
        s = exp(pyrand.uniform(log(scale[0]), log(scale[1])))
        m = np.array([[cos(a), -sin(a)], [sin(a), cos(a)]], "f") / s
        result = get_patch(page, shape, (y, x), m=m, order=1)
        c = random.randint(0, 3) if rotate else 0
        rotated = ndi.rotate(result, 90 * c, order=1).clip(0, 1)
        yield rotated, c


def rot_pipe(source, **kw):
    for (page,) in source:
        yield from rot_samples(page, **kw)


def make_loader(
    urls,
    batch_size=16,
    extensions="nrm.jpg;image.png;framed.png;page.png;png;page.jpg;jpg;jpeg",
    shuffle=0,
    num_workers=4,
    pipe=rot_pipe,
    invert="Auto",
    limit=-1,
):
    training = (
        wds.WebDataset(urls)
        .shuffle(shuffle)
        .decode("l")
        .to_tuple(extensions)
        .map_tuple(lambda image: utils.autoinvert(image, invert))
        .then(pipe)
        .shuffle(shuffle)
    )
    if limit > 0:
        training = wds.ResizedDataset(training, limit, limit)
    return DataLoader(training, batch_size=batch_size, num_workers=num_workers)


@public
class PageOrientation:
    def __init__(self, fname, check=True, device=None):
        self.device = utils.device(device)
        self.model = loading.load_only_model(fname)
        self.check = check
        self.debug = int(os.environ.get("DEBUG_PAGEORIENTATION", 0))

    def orientation(self, page, npatches=200, bs=50):
        if self.check:
            assert np.mean(page) < 0.5
        try:
            self.model.to(self.device)
            self.model.eval()
            patches = rot_samples(page, rotate=False, alpha=(0, 0))
            result = []
            while True:
                batch = [x[0] for x in islice(patches, bs)]
                if len(batch) == 0:
                    break
                batch = np.array(batch)
                inputs = torch.tensor(batch).unsqueeze(1).to(self.device)
                with torch.no_grad():
                    outputs = self.model(inputs).softmax(1).cpu().detach()
                result.append(outputs)
                if self.debug:
                    for i in range(len(inputs)):
                        v = list((outputs[0].numpy() * 100).astype(int))
                        plt.ion()
                        plt.imshow(inputs[i, 0].detach().cpu().numpy())
                        plt.title(
                            str(i) + ": " + repr(inputs.shape) + " " + repr(v)
                        )
                        plt.ginput(1, 1000.0)
                    pass
            self.last = torch.cat(result)
            self.hist = self.last.mean(0)
            return int(self.hist.argmax()) * 90
        finally:
            self.model.cpu()

    def make_upright(self, page):
        angle = self.orientation(page)
        return ndi.rotate(page, -angle)


@app.command()
def train(
    urls: List[str],
    nsamples: int = 1000000,
    num_workers: int = 8,
    replicate: int = 1,
    bs: int = 64,
    prefix: str = "rot",
    lrfun="0.3**(3+n//5000000)",
    log_to: str = "",
    model: str = "page_orientation_210113",
    extensions: str = default_extensions,
    display: float = 0.0,
    invert: str = "Auto",
    device: str = None,
):
    device = utils.device(device)
    logger = slog.Logger(fname=log_to, prefix=prefix)
    logger.save_config(dict(argv=sys.argv))
    model = loading.load_or_construct_model(model)
    model.to(device)
    print(model)
    urls = urls * replicate
    training = make_loader(
        urls,
        shuffle=10000,
        num_workers=num_workers,
        batch_size=bs,
        extensions=extensions,
        invert=invert,
        pipe=rot_pipe,
    )
    criterion = nn.CrossEntropyLoss().to(device)
    lrfun = eval(f"lambda n: {lrfun}")
    lr = lrfun(0)
    optimizer = optim.SGD(model.parameters(), lr=lr)
    count = 0
    losses = []
    errs = []

    def save():
        avgloss = np.mean(losses[-100:])
        logger.save_ocrmodel(model, step=count, loss=avgloss)
        print("\nsaved at", count)

    schedule = utils.Schedule()

    for patches, targets in utils.repeatedly(training):
        if count > nsamples:
            break
        if len(patches) < 2:
            print("skipping small batch", file=sys.stderr)
            continue
        patches = patches.type(torch.float).unsqueeze(1).to(device)
        optimizer.zero_grad()
        outputs = model(patches)
        loss = criterion(outputs, targets.to(device))
        loss.backward()
        optimizer.step()
        count += len(patches)
        losses.append(float(loss))
        probs = outputs.detach().cpu().softmax(1)
        pred = probs.argmax(1)
        erate = (pred != targets).sum() * 1.0 / len(pred)
        errs.append(erate)
        if schedule("info", 60, initial=True):
            print(count, np.mean(losses[-50:]), np.mean(errs[-50:]), lr, flush=True)
        if schedule("log", 15 * 60):
            avgloss = np.mean(losses[-100:])
            logger.scalar(
                "train/loss",
                avgloss,
                step=count,
                json=dict(lr=lr),
            )
            logger.flush()
        if display > 0 and schedule("display", display):
            plt.ion()
            plt.imshow(patches[0, 0].detach().cpu().numpy())
            plt.title(f"{targets[0]} {list((100*probs[0].numpy()).astype(int))}")
            plt.ginput(1, 0.001)
        if schedule("save", 15 * 60):
            save()
        if lrfun(count) != lr:
            lr = lrfun(count)
            optimizer = optim.SGD(model.parameters(), lr=lr)

    save()


@app.command()
def correct(
    urls: List[str],
    output: str = "/dev/null",
    model: str = "",
    extensions: str = default_extensions,
    nsamples: int = 999999999,
    invert: str = "Auto",
):
    assert model != ""
    rotest = PageOrientation(model)
    dataset = wds.WebDataset(urls).decode("l").to_tuple("__key__ " + extensions)
    sink = wds.TarWriter(output)
    for key, image in islice(dataset, nsamples):
        image = utils.autoinvert(image, invert)
        rot = rotest.orientation(image) if rotest else None
        print(key, image.shape, rot)
        rotated = ndi.rotate(image, -rot) if rot != 0 else image
        result = dict(__key__=key, jpg=rotated)
        sink.write(result)


@app.command()
def noop():
    pass


if __name__ == "__main__":
    app()
