import torch
import torch.utils.data
from torch import nn, optim
from torch.nn import functional as F
from torchvision import datasets, transforms
from torchvision.utils import save_image
import numpy as np
import os
import time
from argparse import ArgumentParser


class ContextEncoder(nn.Module):
    def __init__(self):
        super(ContextEncoder, self).__init__()
        self.layer1 = nn.Linear(3, 128)
        self.layer2 = nn.Linear(128, 128)
        self.layer3 = nn.Linear(128, 64)

    def forward(self, x):
        x = F.relu(self.layer1(x))
        x = F.relu(self.layer2(x))
        return self.layer3(x)


class ContextToLatentDistribution(nn.Module):
    def __init__(self):
        super(ContextToLatentDistribution, self).__init__()
        self.mu_layer = nn.Linear(64, 64)
        self.logvar_layer = nn.Linear(64, 64)

    def forward(self, x):
        return self.mu_layer(x), self.logvar_layer(x)


class Decoder(nn.Module):
    def __init__(self):
        super(Decoder, self).__init__()
        self.layer1 = nn.Linear(64 + 2, 32)
        self.layer2 = nn.Linear(32, 16)
        self.layer3 = nn.Linear(16, 1)

    def forward(self, x):
        x = F.relu(self.layer1(x))
        x = F.relu(self.layer2(x))
        x = self.layer3(x)
        return torch.sigmoid(x)  # for mnist


def save_model(models_path, model_name, encoder, context_to_latent_dist, decoder, device):
    file_path = os.path.join(models_path, model_name)
    if not os.path.exists(models_path):
        os.makedirs(models_path)
    model_states_dict = {"encoder": encoder.cpu().state_dict(),
                         "context_to_latent_dist": context_to_latent_dist.cpu().state_dict(),
                         "decoder": decoder.cpu().state_dict()}
    torch.save(model_states_dict, file_path)
    encoder = encoder.to(device)
    decoder = decoder.to(device)
    context_to_latent_dist = context_to_latent_dist.to(device)
    print('Saved state dicts to {}'.format(file_path))


def load_models(file_path, encoder, context_to_latent_dist, decoder):
    dict = torch.load(file_path)
    encoder.load_state_dict(dict["encoder"])
    context_to_latent_dist.load_state_dict(dict["context_to_latent_dist"])
    decoder.load_state_dict(dict["decoder"])


def random_sampling(batch, grid, h=28, w=28):
    '''

    :param batch:
    :param grid:
    :param h:
    :param w:
    :return: encoder_input size (bsize,784,3) , mask size (bsize,784)
    '''
    # batch bsize * 1 * 28 * 28
    batch_size = batch.size(0)

    batch = batch.view(batch_size, -1)  # bsize * 784
    ps = torch.rand(batch_size, device=batch.device).unsqueeze(1).expand(batch_size, h * w)
    mask = torch.rand(batch.size(), device=batch.device)
    mask = (mask >= ps).float()  # bsize * 784

    grid = grid.unsqueeze(0).expand(batch_size, h * w, 2)

    return torch.cat([batch.unsqueeze(-1), grid], dim=-1), mask


def kl_normal(params1, params2):
    mu1, var1 = params1
    var1 = var1.exp()
    mu2, var2 = params2
    var2 = var2.exp()
    element_wise = 0.5 * (torch.log(var2) - torch.log(var1) + var1 / var2 + (mu1 - mu2).pow(2) / var2 - 1)
    kl = element_wise.sum(-1)
    return kl


def sample_z(z_params):
    mu, var = z_params
    var = var.exp()
    sample = torch.randn(mu.shape).to(mu.device)
    z = mu + (torch.sqrt(var) * sample)
    return z


def train(context_encoder, context_to_dist, decoder, train_loader, optimizer, n_epochs, device, batch_size, save_path,
          h=28,
          w=28):
    context_encoder.train()
    decoder.train()
    xs = np.linspace(0, 1, h)
    ys = np.linspace(0, 1, w)
    xx, yy = np.meshgrid(xs, ys)
    grid = torch.tensor(np.stack([xx, yy], axis=-1)).float().to(device).view(h * w, 2)  # size 784*2

    for epoch in range(n_epochs):
        epoch_loss = 0.0
        running_loss = 0.0
        last_log_time = time.time()
        for batch_idx, (batch, _) in enumerate(train_loader):
            batch = batch.to(device)
            if ((batch_idx % 100) == 0) and batch_idx > 1:
                print("epoch {} | batch {} | mean running loss {:.2f} | {:.2f} batch/s".format(epoch, batch_idx,
                                                                                               running_loss / 100,
                                                                                               100 / (
                                                                                                       time.time() - last_log_time)))
                last_log_time = time.time()
                running_loss = 0.0

            context_data, mask = random_sampling(batch=batch, grid=grid, h=h, w=w)
            # context data size (bsize,h*w,3) with 3 = (pixel value, coord_x,coord_y)

            context_full = context_encoder(context_data)  # size bsize,h*w,d with d =hidden size

            mask = mask.unsqueeze(-1)  # size bsize * 784 * 1
            r_masked = (context_full * mask).sum(dim=1) / (1 + mask.sum(dim=1))  # bsize * hidden_size
            r_full = context_full.mean(dim=1)
            # print("relative diff between masked and full {:.2f}".format(torch.norm(r_masked-r_full)/torch.norm(r_full)))

            ## compute loss
            z_params_full = context_to_dist(r_full)
            z_params_masked = context_to_dist(r_masked)
            z_full = sample_z(z_params_full)  # size bsize * hidden
            z_full = z_full.unsqueeze(1).expand(-1, h * w, -1)

            # resize context to have one context per input coordinate
            grid_input = grid.unsqueeze(0).expand(batch_size, -1, -1)
            target_input = torch.cat([z_full, grid_input], dim=-1)

            reconstructed_image = decoder.forward(target_input)

            reconstruction_loss = (F.binary_cross_entropy(reconstructed_image, batch.view(batch_size, h * w, 1),
                                                          reduction='none') * (1 - mask)).sum(dim=1).mean()

            kl_loss = kl_normal(z_params_full, z_params_masked).mean()
            if batch_idx % 100 == 0:
                print("reconstruction {:.2f} | kl {:.2f}".format(reconstruction_loss, kl_loss))

            loss = reconstruction_loss + kl_loss
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

            # add loss
            running_loss += loss.item()
            epoch_loss += loss.item()

        print("Epoch loss : {}".format(epoch_loss / len(train_loader)))
    save_model(args.models_path, "NP_model_epoch_{}.pt".format(args.epochs), context_encoder, context_to_dist, decoder, device)
    return


def main(args):
    use_cuda = torch.cuda.is_available()
    device = torch.device("cuda" if use_cuda else "cpu")

    train_loader = torch.utils.data.DataLoader(

        datasets.MNIST('../data', train=True, download=True,
                       transform=transforms.Compose([
                           transforms.ToTensor(),
                           transforms.Lambda(lambda x: (x > .5).float())
                       ])),
        batch_size=args.bsize, shuffle=True)

    test_loader = torch.utils.data.DataLoader(
        datasets.MNIST('../data', train=False, transform=transforms.Compose([
            transforms.ToTensor(),
            transforms.Lambda(lambda x: (x > .5).float())
        ])),
        batch_size=args.bsize, shuffle=True)

    context_encoder = ContextEncoder()
    context_to_dist = ContextToLatentDistribution()
    decoder = Decoder()

    if args.resume_file is not None:
        load_models(args.resume_file,context_encoder,context_to_dist,decoder)
    context_encoder = context_encoder.to(device)
    decoder = decoder.to(device)
    context_to_dist = context_to_dist.to(device)
    full_model_params = list(context_encoder.parameters()) + list(decoder.parameters()) + list(
        context_to_dist.parameters())
    optimizer = optim.Adam(full_model_params, lr=args.lr)

    train(context_encoder, context_to_dist, decoder, train_loader, optimizer, args.epochs, device, args.bsize,
          args.models_path)


parser = ArgumentParser()
parser.add_argument("--models_path", type=str, default="models/")
parser.add_argument("--save_model", type=int, default=1)
parser.add_argument("--lr", type=float, default=1e-3)
parser.add_argument("--epochs", type=int, default=10)
parser.add_argument("--bsize", type=int, default=32)
parser.add_argument("--resume_file",type=str,default=None)

if __name__ == '__main__':
    args = parser.parse_args()
    main(args)
