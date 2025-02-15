"""
TITLE
Module for training and generating data from conditional and joint distributions
using WGANs.

Author: Jonas Metzger and Evan Munro
TITLE
"""

import torch
import math
import torch.nn as nn
import torch.nn.functional as F
from torch.utils import data as D
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from time import time

def collect_features(data, cate_word_variable):
    """
        Returns the number of different values of a specificed categorical word variable.
    """
    features_dict = {}
    for obs in data[cate_word_variable]:
        try:
            tokens = obs.split(',')
            for token in tokens:
                features_dict[token] = features_dict.setdefault(token, 0) + 1
        except:
            pass
    
    return features_dict

def get_top_features(features, n_top_features = 0):
    """
        Returns the top n_top_features from features dict
    """

    return dict(sorted(features.items(), key = lambda skill: skill[1])[-n_top_features:])


def make_feature_vector(observation, top_features):
    """
        Returns tensor with one-hot embedding for top_features
    """
    dim = len(top_features)
    x = np.zeros(dim)
    try:
        tokens = observation.split(',')
        # print(posting_tokens)
        # print(top_skills)
        for idx, skill in enumerate(top_features.keys()):
            if skill in tokens:
                x[idx] = 1
        return torch.from_numpy(x).float()
    except:
            pass
    

class DataWrapper(object):
    """Class for processing raw training data for training Wasserstein GAN

    Parameters
    ----------
    df: pandas.DataFrame
        Training data frame, includes both variables to be generated, and
        variables to be conditioned on
    continuous_vars: list
        List of str of continuous variables to be generated
    categorical_vars: list
        List of str of categorical variables to be generated
    context_vars: list
        List of str of variables that are conditioned on for cWGAN
    continuous_lower_bounds: dict
        Key is element of continuous_vars, value is lower limit on that variable.
    continuous_upper_bounds: dict
        Key is element of continuous_vars, value is upper limit on that variable.


    ATTRIBUTES
    Attributes
    ----------
    variables: dict
        Includes lists of names of continuous, categorical and context variables
    means: list
        List of means of continuous and context variables
    stds: list
        List of float of standard deviation of continuous and context variables
    cat_dims: list
        List of dimension of each categorical variable
    cat_labels: list
        List of labels of each categorical variable
    cont_bounds: torch.tensor
        formatted lower and upper bounds of continuous variables
    ATTRIBUTES
    """
    def __init__(self, df, continuous_vars=[], categorical_vars=[], context_vars=[],
                 continuous_lower_bounds = dict(), continuous_upper_bounds = dict()):
        variables = dict(continuous=continuous_vars,
                         categorical=categorical_vars,
                         context=context_vars)
        self.variables = variables
        continuous, context = [torch.tensor(np.array(df[variables[_]])).to(torch.float) for _ in ("continuous", "context")]
        
        
        # ! This is where the vectors are created: insert make_feature_vector here
        # ! print(f"continuous: {continuous}")
        # ! print(f"context: {context}")
        
        self.means = [x.mean(0, keepdim=True) for x in (continuous, context)]
        self.stds  = [x.std(0,  keepdim=True) + 1e-5 for x in (continuous, context)]

        # print(f"means: {self.means}")
        # print(f"stds: {self.stds}")

        self.cat_dims = [df[v].nunique() for v in variables["categorical"]]
        # print(f"cat_dims: {self.cat_dims}")

        #*
        self.words_to_int = {}
        self.int_to_words = {}

        self.cat_labels = []
        for v in variables["categorical"]:
            if not isinstance(df[v][0], float):
                feature_labels = pd.get_dummies(df[v]).columns
                self.words_to_int[v] = {feature: idx for idx, feature in enumerate(feature_labels.to_numpy())}
                self.int_to_words[v] = {idx: feature for idx, feature in enumerate(feature_labels.to_numpy())}
                source = df[v].map(self.words_to_int[v])
            else:
                source = df[v]

            self.cat_labels.append((v, torch.tensor(pd.get_dummies(source).columns.to_numpy()).to(torch.float)))

            
        # print(f"words_to_int table: {self.words_to_int}")
        # print(f"int_to_words table: {self.int_to_words}")
        # print(f"cat labels: {self.cat_labels}")

        #*
        
        self.cont_bounds = [[continuous_lower_bounds[v] if v in continuous_lower_bounds.keys() else -1e8 for v in variables["continuous"]],
                            [continuous_upper_bounds[v] if v in continuous_upper_bounds.keys() else 1e8 for v in variables["continuous"]]]
        self.cont_bounds = (torch.tensor(self.cont_bounds).to(torch.float) - self.means[0]) / self.stds[0]
        self.df0 = df[continuous_vars+categorical_vars].iloc[0:1].copy() # saves first row of generated vars to infer types during generation

    def preprocess(self, df):
        """
        Scale training data for training in WGANs

        Parameters
        ----------
        df: pandas.DataFrame
            raw training data
        Returns
        -------
        x: torch.tensor
            training data to be generated by WGAN

        context: torch.tensor
            training data to be conditioned on by WGAN
        """
        x, context = [torch.tensor(np.array(df[self.variables[_]])).to(torch.float) for _ in ("continuous", "context")]
        x, context = [(x-m)/s for x,m,s in zip([x, context], self.means, self.stds)]

        

        if len(self.variables["categorical"]) > 0:
            #!This is where equivalent of one-hot encodings are made
            # print(f"categorical looks like: {pd.get_dummies(df[self.variables['categorical']], columns=self.variables['categorical'])}")

            categorical = torch.tensor(pd.get_dummies(df[self.variables["categorical"]], columns=self.variables["categorical"]).to_numpy())

            
            x = torch.cat([x, categorical.to(torch.float)], -1)
        total = torch.cat([x, context], -1)
        if not torch.all(total==total):
            raise RuntimeError("It looks like there are NaNs your data, at least after preprocessing. This is currently not supported!")

        
        return x, context

    def deprocess(self, x, context, n_features = 1):
        """
        Unscale tensors from WGAN output to original scale

        Parameters
        ----------
        x: torch.tensor
            Generated data
        context: torch.tensor
            Data conditioned on
        Returns
        -------
        df: pandas.DataFrame
            DataFrame with data converted back to original scale
        """
        continuous, categorical = x.split((self.means[0].size(-1), sum(self.cat_dims)), -1)
        continuous, context = [x*s+m for x,m,s in zip([continuous, context], self.means, self.stds)]
        if categorical.size(-1) > 0:

            # #*
            # new_categorical = []
            # for p, l in zip(categorical.split(self.cat_dims, -1), self.cat_labels):
            #     if l[0] in self.cat_encoding_table:
            #         encoding = list(self.cat_encoding_table[l[0]].keys())
            #         new_categorical.append(l[torch.multinomial(p,1)])
            #         print(f"addend: {categorical[-1]}")
            #     # else:
            #         # categorical.append(l[])
            new_categorical = []
            for p, l in zip(categorical.split(self.cat_dims, -1), self.cat_labels):
                if l[0] in self.int_to_words:
                    # print(f"test4: {[l[1][i] for i in [torch.multinomial(p, n_features)]][0]}")
                    new_categorical.append([l[1][i] for i in [torch.multinomial(p, n_features)]][0])
                else:
                    # print(f"size: {p.size()}")
                    # print(f'test3: {[[0] for _ in range(1, n_top_features)]}')
                    # print(f'test2: {torch.tensor([[0] for _ in range(1, n_top_features)])}')
                    # print(f'test: {}')
                    new_categorical.append(torch.cat([l[1][torch.multinomial(p, 1)], torch.tensor([[0] for _ in range(1, n_features)] * p.size()[0])], -1 ))
            categorical = torch.cat(new_categorical, -1)
            # print(f"categorical: {categorical}")
            #*
        new_variables_categorical = []
        for idx, var in enumerate(self.variables["categorical"]):
            new_variables_categorical.append(var)
            filler_vars = [var + str(i) for i in range(1, n_features)]
            new_variables_categorical += filler_vars

            if var in self.words_to_int:
                for f_var in filler_vars:
                    self.words_to_int[f_var] = self.words_to_int[var]
                    self.int_to_words[f_var] = self.int_to_words[var]
                
            # print(f"test 5: {new_variables_categorical}")
        self.variables["categorical"] = new_variables_categorical

        df = pd.DataFrame(dict(zip(self.variables["continuous"] + self.variables["categorical"] +  self.variables["context"],
                                   torch.cat([continuous, categorical, context], -1).detach().t())))
        
        # print(df.head())
        # print(f"new tens: {torch.cat([continuous, categorical, context], -1).detach()}")
        for cate_word in self.words_to_int:
            df[cate_word] = df[cate_word].map(self.int_to_words[cate_word])

        return df

    def apply_generator(self, generator, df, n_features = 1):
        """
        Replaces or inserts columns in DataFrame that are generated by the generator, of
        size equal to the number of rows in the DataFrame that is passed

        Parameters
        ----------
        df: pandas.DataFrame
            Must contain columns listed in self.variables["context"], which the generator
            will be conditioned on. Even without context vars, len(df) is used to infer the
            desired sample size, so you need to supply at least pd.DataFrame(index=range(n))
        generator: wgan_model.Generator
            Trained generator for simulating data
        Returns
        -------
        pandas.DataFrame
            Original DataFrame with columns replaced by generated data where possible.
        """
        # replaces or inserts columns in df with data from generator wherever possible
        generator.to("cpu")
        updated = self.variables["continuous"] + self.variables["categorical"]
        df = df.drop(updated, axis=1, errors="ignore").reset_index(drop=True).copy()
        df = self.df0.sample(len(df), replace=True).reset_index(drop=True).join(df)
        original_columns = df.columns
        x, context = self.preprocess(df)
        x_hat = generator(context)
        df_hat = self.deprocess(x_hat, context, n_features)

        print(f"original columns looks like: {original_columns}")
        # not_updated = [col for col in list(df_hat.columns) if col not in updated]
        
        new_columns = pd.Index([col for col in df_hat.columns])
        # df_hat = df_hat.drop(not_updated, axis=1).reset_index(drop=True)
        df = df.drop(updated, axis=1).reset_index(drop=True)
        # print(f"df now looks like: {df}")
        # print(f"df_hat now looks like: {df_hat}")
        return df_hat.join(df)[new_columns]
        return df_hat

    def apply_critic(self, critic, df, colname="critic"):
        """
        Adds column with critic output for each row of the provided Dataframe

        Parameters
        ----------
        critic: wgan_model.Critic
        df: pandas.DataFrame
        colname: str
            Name of column to add to df with critic output value
        Returns
        -------
        pandas.DataFrame
        """
        critic.to("cpu")
        x, context = self.preprocess(df)
        c = critic(x, context).detach()
        if colname in list(df.columns): df = df.drop(colname, axis=1)
        df.insert(0, colname, c[:, 0].numpy())
        return df


class OAdam(torch.optim.Optimizer):
    """Implements optimistic Adam algorithm.
    Copied from: https://github.com/georgepar/optimistic-adam
    It has been proposed in `Training GANs with Optimism` (https://arxiv.org/abs/1711.00141)
    
    Parameters
    ----------
    see torch.optim.Adam
    Returns
    -------
    torch.optim.Optimizer
    """
    def __init__(self, params, lr=1e-3, betas=(0.9, 0.999), eps=1e-8,
                 weight_decay=0, amsgrad=False):
        if not 0.0 <= lr:
            raise ValueError("Invalid learning rate: {}".format(lr))
        if not 0.0 <= eps:
            raise ValueError("Invalid epsilon value: {}".format(eps))
        if not 0.0 <= betas[0] < 1.0:
            raise ValueError("Invalid beta parameter at index 0: {}".format(betas[0]))
        if not 0.0 <= betas[1] < 1.0:
            raise ValueError("Invalid beta parameter at index 1: {}".format(betas[1]))
        defaults = dict(lr=lr, betas=betas, eps=eps,
                        weight_decay=weight_decay, amsgrad=amsgrad)
        super(OAdam, self).__init__(params, defaults)

    def __setstate__(self, state):
        super(Adam, self).__setstate__(state)
        for group in self.param_groups:
            group.setdefault('amsgrad', False)

    def step(self, closure=None):
        """Performs a single optimization step.
        Arguments:
            closure (callable, optional): A closure that reevaluates the model
                and returns the loss.
        """
        loss = None
        if closure is not None:
            loss = closure()
        for group in self.param_groups:
            for p in group['params']:
                if p.grad is None:
                    continue
                grad = p.grad.data
                if grad.is_sparse:
                    raise RuntimeError('Adam does not support sparse gradients, please consider SparseAdam instead')
                amsgrad = group['amsgrad']
                state = self.state[p]
                # State initialization
                if len(state) == 0:
                    state['step'] = 0
                    # Exponential moving average of gradient values
                    state['exp_avg'] = torch.zeros_like(p.data)
                    # Exponential moving average of squared gradient values
                    state['exp_avg_sq'] = torch.zeros_like(p.data)
                    if amsgrad:
                        # Maintains max of all exp. moving avg. of sq. grad. values
                        state['max_exp_avg_sq'] = torch.zeros_like(p.data)
                exp_avg, exp_avg_sq = state['exp_avg'], state['exp_avg_sq']
                if amsgrad:
                    max_exp_avg_sq = state['max_exp_avg_sq']
                beta1, beta2 = group['betas']
                state['step'] += 1
                if group['weight_decay'] != 0:
                    grad.add_(group['weight_decay'], p.data)
                bias_correction1 = 1 - beta1 ** state['step']
                bias_correction2 = 1 - beta2 ** state['step']
                step_size = group['lr'] * math.sqrt(bias_correction2) / bias_correction1
                # Optimistic update :)
                p.data.addcdiv_(step_size, exp_avg, exp_avg_sq.sqrt().add(group['eps']))
                # Decay the first and second moment running average coefficient
                exp_avg.mul_(beta1).add_(1 - beta1, grad)
                exp_avg_sq.mul_(beta2).addcmul_(1 - beta2, grad, grad)
                if amsgrad:
                    # Maintains the maximum of all 2nd moment running avg. till now
                    torch.max(max_exp_avg_sq, exp_avg_sq, out=max_exp_avg_sq)
                    # Use the max. for normalizing running avg. of gradient
                    denom = max_exp_avg_sq.sqrt().add_(group['eps'])
                else:
                    denom = exp_avg_sq.sqrt().add_(group['eps'])
                p.data.addcdiv_(-2.0 * step_size, exp_avg, denom)
        return loss   
    

class Specifications(object):
    """Class used to set up WGAN training specifications before training
    Generator and Critic.

    Parameters
    ----------
    data_wrapper: wgan_model.DataWrapper
        Object containing details on data frame to be trained
    optimizer: torch.optim.Optimizer
        The torch.optim.Optimizer object used for training the networks, per default torch.optim.Adam
    critic_d_hidden: list
        List of int, length equal to the number of hidden layers in the critic,
        giving the size of each hidden layer.
    critic_dropout: float
        Dropout parameter for critic (see Srivastava et al 2014)
    critic_steps: int
        Number of critic training steps taken for each generator training step
    critic_lr: float
        Initial learning rate for critic
    critic_gp_factor: float
        Weight on gradient penalty for critic loss function
    generator_d_hidden: list
        List of int, length equal to the number of hidden layers in generator,
        giving the size of each hidden layer.
    generator_dropout: float
        Dropout parameter for generator (See Srivastava et al 2014)
    generator_lr: float
        Initial learning rate for generator
    generator_d_noise: int
        The dimension of the noise input to the generator. Default sets to the
        output dimension of the generator.
    generator_optimizer: torch.optim.Optimizer
        The torch.optim.Optimizer object used for training the generator network if different from "optimizer", per default the same
    max_epochs: int
        The number of times to train the network on the whole dataset.
    batch_size: int
        The batch size for each training iteration.
    test_set_size: int
        Holdout test set for calculating out of sample wasserstein distance.
    load_checkpoint: str
        Filepath to existing model weights to start training from.
    save_checkpoint: str
        Filepath of folder to save model weights every save_every iterations
    save_every: int
        If save_checkpoint is not None, then how often to save checkpoint of model
        weights during training.
    print_every: int
        How often to print training status during training.
    device: str
        Either "cuda" if GPU is available or "cpu" if not

    ATTRIBUTES
    Attributes
    ----------
    settings: dict
        Contains the neural network-related settings for training
    data: dict
        Contains settings related to the data dimension and bounds
    ATTRIBUTES
    """
    def __init__(self, data_wrapper,
                 optimizer = torch.optim.Adam,
                 critic_d_hidden = [128,128,128],
                 critic_dropout = 0,
                 critic_steps = 15,
                 critic_lr = 1e-4,
                 critic_gp_factor = 5,
                 generator_d_hidden = [128,128,128],
                 generator_dropout = 0.1,
                 generator_lr = 1e-4,
                 generator_d_noise = "generator_d_output",
                 generator_optimizer = "optimizer",
                 max_epochs = 1000,
                 batch_size = 32,
                 test_set_size = 16,
                 load_checkpoint = None,
                 save_checkpoint = None,
                 save_every = 100,
                 print_every = 200,
                 device = "cuda" if torch.cuda.is_available() else "cpu",
                 ):

        self.settings = locals()
        del self.settings["self"], self.settings["data_wrapper"]
        d_context = len(data_wrapper.
                        variables["context"])
        d_cont = len(data_wrapper.variables["continuous"])
        d_x = d_cont + sum(data_wrapper.cat_dims)
        if generator_d_noise == "generator_d_output":
            self.settings.update(generator_d_noise = d_x)
        self.data = dict(d_context=d_context, d_x=d_x,
                         cat_dims=data_wrapper.cat_dims,
                         cont_bounds=data_wrapper.cont_bounds)

        print("settings:", self.settings)


class Generator(nn.Module):
    """
    torch.nn.Module class for generator network in WGAN

    Parameters
    ----------
    specifications: wgan_model.Specifications
        parameters for training WGAN

    ATTRIBUTES
    Attributes
    ----------
    cont_bounds: torch.tensor
        formatted lower and upper bounds of continuous variables
    cat_dims: list
        Dimension of each categorical variable
    d_cont: int
        Total dimension of continuous variables
    d_cat: int
        Total dimension of categorical variables
    d_noise: int
        Dimension of noise input to generator
    layers: torch.nn.ModuleList
        Dense neural network layers making up the generator
    dropout: torch.nn.Dropout
        Dropout layer based on specifications
    ATTRIBUTES
    """
    def __init__(self, specifications):
        super().__init__()
        s, d = specifications.settings, specifications.data
        self.cont_bounds = d["cont_bounds"]
        self.cat_dims = d["cat_dims"]
        self.d_cont = self.cont_bounds.size(-1)
        self.d_cat = sum(d["cat_dims"])
        self.d_noise = s["generator_d_noise"]
        d_in = [self.d_noise + d["d_context"]] + s["generator_d_hidden"]
        d_out = s["generator_d_hidden"] + [self.d_cont + self.d_cat]
        self.layers = nn.ModuleList([nn.Linear(i, o) for i, o in zip(d_in, d_out)])
        self.dropout = nn.Dropout(s["generator_dropout"])

    def _transform(self, hidden):
        continuous, categorical = hidden.split([self.d_cont, self.d_cat], -1)
        if continuous.size(-1) > 0: # apply bounds to continuous
            bounds = self.cont_bounds.to(hidden.device)
            continuous = torch.stack([continuous, bounds[0:1].expand_as(continuous)]).max(0).values
            continuous = torch.stack([continuous, bounds[1:2].expand_as(continuous)]).min(0).values
        if categorical.size(-1) > 0: # renormalize categorical
            categorical = torch.cat([F.softmax(x, -1) for x in categorical.split(self.cat_dims, -1)], -1)
        return torch.cat([continuous, categorical], -1)

    def forward(self, context):
        """
            Run generator model

        Parameters
        ----------
        context: torch.tensor
            Variables to condition on

        Returns
        -------
        torch.tensor
        """
        noise = torch.randn(context.size(0), self.d_noise).to(context.device)
        x = torch.cat([noise, context], -1)
        for layer in self.layers[:-1]:
            x = self.dropout(F.relu(layer(x)))
        return self._transform(self.layers[-1](x))


class Critic(nn.Module):
    """
    torch.nn.Module for critic in WGAN framework

    Parameters
    ----------
    specifications: wgan_model.Specifications

    ATTRIBUTES
    Attributes
    ----------
    layers: torch.nn.ModuleList
        Dense neural network making up the critic
    dropout: torch.nn.Dropout
        Dropout layer applied between each of hidden layers
    ATTRIBUTES
    """
    def __init__(self, specifications):
        super().__init__()
        s, d = specifications.settings, specifications.data
        d_in = [d["d_x"] + d["d_context"]] + s["critic_d_hidden"]
        d_out = s["critic_d_hidden"] + [1]
        self.layers = nn.ModuleList([nn.Linear(i, o) for i, o in zip(d_in, d_out)])
        self.dropout = nn.Dropout(s["critic_dropout"])

    def forward(self, x, context):
        """
        Run critic model

        Parameters
        ----------
        x: torch.tensor
            Real or generated data
        context: torch.tensor
            Data conditioned on

        Returns
        -------
        torch.tensor
        """
        x = torch.cat([x, context], -1)
        for layer in self.layers[:-1]:
            x = self.dropout(F.relu(layer(x)))
        return self.layers[-1](x)

    def gradient_penalty(self, x, x_hat, context):
        """
        Calculate gradient penalty

        Parameters
        ----------
        x: torch.tensor
            real data
        x_hat: torch.tensor
            generated data
        context: torch.tensor
            context data

        Returns
        -------
        torch.tensor
        """
        alpha = torch.rand(x.size(0)).unsqueeze(1).to(x.device)
        interpolated = x * alpha + x_hat * (1 - alpha)
        interpolated = torch.autograd.Variable(interpolated.detach(), requires_grad=True)
        critic = self(interpolated, context)
        gradients = torch.autograd.grad(critic, interpolated, torch.ones_like(critic),
                                        retain_graph=True, create_graph=True, only_inputs=True)[0]
        penalty = F.relu(gradients.norm(2, dim=1) - 1).mean()             # one-sided
        # penalty = (gradients.norm(2, dim=1) - 1).pow(2).mean()          # two-sided
        return penalty


def train(generator, critic, x, context, specifications, penalty=None):
    """
    Function for training generator and critic in conditional WGAN-GP
    If context is empty, trains a regular WGAN-GP. See Gulrajani et al 2017
    for details on training procedure.

    Parameters
    ----------
    generator: wgan_model.Generator
        Generator network to be trained
    critic: wgan_model.Critic
        Critic network to be trained
    x: torch.tensor
        Training data for generated data
    context: torch.tensor
        Data conditioned on for generating data
    specifications: wgan_model.Specifications
        Includes all the tuning parameters for training
    """
    # setup training objects
    s = specifications.settings
    start_epoch, step, description, device, t = 0, 1, "", s["device"], time()
    generator.to(device), critic.to(device)
    opt_generator = s["optimizer"] if s["generator_optimizer"]=="optimizer" else s["generator_optimizer"]
    opt_generator = opt_generator(generator.parameters(), lr=s["generator_lr"])
    opt_critic = s["optimizer"](critic.parameters(), lr=s["critic_lr"])
    train_batches, test_batches = D.random_split(D.TensorDataset(x, context), (x.size(0)-s["test_set_size"], s["test_set_size"]))
    train_batches, test_batches = (D.DataLoader(d, s["batch_size"], shuffle=True) for d in (train_batches, test_batches))

    # load checkpoints
    if s["load_checkpoint"]:
        cp = torch.load(s["load_checkpoint"])
        generator.load_state_dict(cp["generator_state_dict"])
        opt_generator.load_state_dict(cp["opt_generator_state_dict"])
        critic.load_state_dict(cp["critic_state_dict"])
        opt_critic.load_state_dict(cp["opt_critic_state_dict"])
        start_epoch, step = cp["epoch"], cp["step"]
    # start training
    try:
        for epoch in range(start_epoch, s["max_epochs"]):
            # train loop
            WD_train, n_batches = 0, 0
            for x, context in train_batches:
                x, context = x.to(device), context.to(device)
                generator_update = step % s["critic_steps"] == 0
                for par in critic.parameters():
                    par.requires_grad = not generator_update
                for par in generator.parameters():
                    par.requires_grad = generator_update
                if generator_update:
                    generator.zero_grad()
                else:
                    critic.zero_grad()
                x_hat = generator(context)
                critic_x_hat = critic(x_hat, context).mean()
                if not generator_update:
                    critic_x = critic(x, context).mean()
                    WD = critic_x - critic_x_hat
                    loss = - WD
                    loss += s["critic_gp_factor"] * critic.gradient_penalty(x, x_hat, context)
                    loss.backward()
                    opt_critic.step()
                    WD_train += WD.item()
                    n_batches += 1
                else:
                    loss = - critic_x_hat
                    if penalty is not None:
                        loss += penalty(x_hat, context)
                    loss.backward()
                    opt_generator.step()
                step += 1
            WD_train /= n_batches
            # test loop
            WD_test, n_batches = 0, 0
            for x, context in test_batches:
                x, context = x.to(device), context.to(device)
                with torch.no_grad():
                    x_hat = generator(context)
                    critic_x_hat = critic(x_hat, context).mean()
                    critic_x = critic(x, context).mean()
                    WD_test += (critic_x - critic_x_hat).item()
                    n_batches += 1
            WD_test /= n_batches
            # diagnostics
            if epoch % s["print_every"] == 0:
                description = "epoch {} | step {} | WD_test {} | WD_train {} | sec passed {} |".format(
                epoch, step, round(WD_test, 2), round(WD_train, 2), round(time() - t))
                print(description)
                t = time()
            if s["save_checkpoint"] and epoch % s["save_every"] == 0:
                torch.save({"epoch": epoch, "step": step,
                            "generator_state_dict": generator.state_dict(),
                            "critic_state_dict": critic.state_dict(),
                            "opt_generator_state_dict": opt_generator.state_dict(),
                            "opt_critic_state_dict": opt_critic.state_dict()}, s["save_checkpoint"])
    except KeyboardInterrupt:
        print("exited gracefully.")


def compare_dfs(df_real, df_fake, scatterplot=dict(x=[], y=[], samples=400, smooth=0),
                table_groupby=[], histogram=dict(variables=[], nrow=1, ncol=1),
                figsize=3,save=False,path=""):
    """
    Diagnostic function for comparing real and generated data from WGAN models.
    Prints out comparison of means, comparisons of standard deviations, and histograms
    and scatterplots.

    Parameters
    ----------
    df_real: pandas.DataFrame
        real data
    df_fake: pandas.DataFrame
        data produced by generator
    scatterplot: dict
        Contains specifications for plotting scatterplots of variables in real and fake data
    table_groupby: list
        List of variables to group mean and standard deviation table by
    histogram: dict
        Contains specifications for plotting histograms comparing marginal densities
        of real and fake data
    save: bool
        Indicate whether to save results to file or print them
    path: string
        Path to save diagnostics for model
    """
    # data prep
    if "source" in list(df_real.columns): df_real = df_real.drop("source", axis=1)
    if "source" in list(df_fake.columns): df_fake = df_fake.drop("source", axis=1)

    
    common_cols = [c for c in df_real.columns if c in df_fake.columns and isinstance(df_fake.iloc[0][c], float)]
    common_cols.append("source")
    df_real.insert(0, "source", "real"), df_fake.insert(0, "source", "fake")
    df_joined = pd.concat([df_real[common_cols], df_fake[common_cols]], axis=0, ignore_index=True)
    df_real, df_fake = df_real.drop("source", axis=1), df_fake.drop("source", axis=1)
    common_cols = [c for c in common_cols if c != "source"]
    # mean and std table

    # print(f"df_joined now looks like : {df_joined}")
    means = df_joined.groupby(table_groupby + ["source"]).mean().round(2).transpose()
    if save:
        means.to_csv(path+"_means.txt",sep=" ")
    else:
        print("-------------comparison of means-------------")
        print(means)

    stds = df_joined.groupby(table_groupby + ["source"]).std().round(2).transpose()

    if save:
        stds.to_csv(path+"_stds.txt",sep=" ")
    else:
        print("-------------comparison of stds-------------")
        print(stds)
    # covariance matrix comparison
    fig1 = plt.figure(figsize=(figsize * 2, figsize * 1))
    s1 = [fig1.add_subplot(1, 2, i) for i in range(1, 3)]
    s1[0].set_xlabel("real")
    s1[1].set_xlabel("fake")
    s1[0].matshow(df_real[common_cols].corr())
    s1[1].matshow(df_fake[common_cols].corr())
    # histogram marginals
    if histogram and len(histogram["variables"]) > 0:
        fig2, axarr2 = plt.subplots(histogram["nrow"], histogram["ncol"],
                                    figsize=(histogram["nrow"]*figsize, histogram["ncol"]*figsize))
        v = 0
        for i in range(histogram["nrow"]):
            for j in range(histogram["ncol"]):
                plot_var, v = histogram["variables"][v], v+1
                axarr2[i][j].hist([df_real[plot_var], df_fake[plot_var]], bins=8, density=1,
                                  histtype='bar', label=["real", "fake"], color=["blue", "red"])
                axarr2[i][j].legend(prop={"size": 10})
                axarr2[i][j].set_title(plot_var)
        if save:
            fig2.savefig(path+'_hist.png')
        else:
            fig2.show()
    # scatterplot grid
    if scatterplot and len(scatterplot["x"]) * len(scatterplot["y"]) > 0:
        df_real_sample = df_real.sample(scatterplot["samples"])
        df_fake_sample = df_fake.sample(scatterplot["samples"])
        x_vars, y_vars = scatterplot["x"], scatterplot["y"]
        fig3 = plt.figure(figsize=(len(x_vars) * figsize, len(y_vars) * figsize))
        s3 = [fig3.add_subplot(len(y_vars), len(x_vars), i + 1) for i in range(len(x_vars) * len(y_vars))]
        for y in y_vars:
            for x in x_vars:
                s = s3.pop(0)
                x_real, y_real = df_real_sample[x].to_numpy(),  df_real_sample[y].to_numpy()
                x_fake, y_fake = df_fake_sample[x].to_numpy(), df_fake_sample[y].to_numpy()
                from math import sqrt,pi
                def fit(xx, yy):
                    xx, yy = torch.tensor(xx).to(torch.float), torch.tensor(yy).to(torch.float)
                    xx = (xx - xx.mean())/ xx.std()
                    bw = 1e-9 + scatterplot["smooth"] # * (xx.max()-xx.min())
                    dist = (xx.unsqueeze(0) - xx.unsqueeze(1)).pow(2)/bw
                    kern = 1/sqrt(2*pi)*torch.exp(-dist**2/2)
                    w = kern / kern.sum(1, keepdim=True)
                    y_hat = w.mm(yy.unsqueeze(1)).squeeze()
                    return y_hat.detach().numpy()
                y_real, y_fake = fit(x_real, y_real), fit(x_fake, y_fake)
                s.scatter(x_real, y_real, color="blue")
                s.scatter(x_fake, y_fake, color="red")
                s.set_ylabel(y)
                s.set_xlabel(x)

        if save:
            fig3.savefig(path+'_scatter.png')
        else:
            fig3.show()

            
def gaussian_similarity_penalty(x_hat, context, eps=1e-4):
    """
    Penalizes generators which can be approximated well by a Gaussian

    Parameters
    ----------
    x_hat: torch.tensor
        generated data
    context: torch.tensor
        context data

    Returns
    -------
    torch.tensor
    """
    x = torch.cat([x_hat, context], dim=1)
    mean = x.mean(0, keepdim=True)
    cov = x.t().mm(x) / x.size(0) - mean.t().mm(mean) + eps * torch.rand_like(x[0]).diag()
    gaussian = torch.distributions.MultivariateNormal(mean.detach(), cov.detach())
    loglik = gaussian.log_prob(x).mean()
    return loglik  


def monotonicity_penalty_kernreg(factor, h=0.1, idx_out=4, idx_in=0, x_min=None, x_max=None, data_wrapper=None):
  """
  Adds Kernel Regression monotonicity penalty.
  Incentivizes monotonicity of the mean of cat(x_hat, context)[:, dim_out] conditional on cat(x_hat, context)[:, dim_in].
  Parameters
  ----------
  x_hat: torch.tensor
      generated data
  context: torch.tensor
      context data
  Returns
  -------
  torch.tensor
  """
  if data_wrapper is not None:
    x_std = torch.cat(data_wrapper.stds, -1).squeeze()[idx_in]
    x_mean = torch.cat(data_wrapper.means, -1).squeeze()[idx_in]
    x_min, x_max = ((x-x_mean)/(x_std+1e-3) for x in (x_min, x_max))
  if x_min is None: x_min = x.min()
  if x_max is None: x_max = x.max()
  def penalty(x_hat, context):
    y, x = (torch.cat([x_hat, context], -1)[:, idx] for idx in (idx_out, idx_in))
    k = lambda x: (1-x.pow(2)).clamp_min(0)
    x_grid = ((x_max-x_min)*torch.arange(20, device=x.device)/20 + x_min).detach()
    W = k((x_grid.unsqueeze(-1) - x)/h).detach()
    W = W/(W.sum(-1, True) + 1e-2)
    y_mean = (W*y).sum(-1).squeeze()
    return (factor * (y_mean[:-1]-y_mean[1:])).clamp_min(0).sum()
  return penalty


def monotonicity_penalty_chetverikov(factor, bound=0, idx_out=4, idx_in=0):
  """
  Adds Chetverikov monotonicity test penalty.
  Incentivizes monotonicity of the mean of cat(x_hat, context)[:, dim_out] conditional on cat(x_hat, context)[:, dim_in].
  Parameters
  ----------
  x_hat: torch.tensor
      generated data
  context: torch.tensor
      context data
  Returns
  -------
  torch.tensor
  """
  def penalty(x_hat, context):
    y, x = (torch.cat([x_hat, context], -1)[:, idx] for idx in (idx_out, idx_in))
    argsort = torch.argsort(x)
    y, x = y[argsort], x[argsort]
    sigma = (y[:-1] - y[1:]).pow(2)
    sigma = torch.cat([sigma, sigma[-1:]])
    k = lambda x: 0.75*F.relu(1-x.pow(2))
    h_max = torch.tensor((x.max()-x.min()).detach()/2).to(x_hat.device)
    n = y.size(0)
    h_min = 0.4*h_max*(np.log(n)/n)**(1/3)
    l_max = int((h_min/h_max).log()/np.log(0.5))
    H = h_max * (torch.tensor([0.5])**torch.arange(l_max)).to(x_hat.device)
    x_dist = (x.unsqueeze(-1) - x) # i, j
    Q = k(x_dist.unsqueeze(-1) / H) # i, j, h
    Q = (Q.unsqueeze(0) * Q.unsqueeze(1)).detach() # i, j, x, h
    y_dist = (y - y.unsqueeze(-1)) # i, j
    sgn = torch.sign(x_dist) * (x_dist.abs() > 1e-8) # i, j
    b = ((y_dist * sgn).unsqueeze(-1).unsqueeze(-1) * Q).sum(0).sum(0) # x, h
    V = ((sgn.unsqueeze(-1).unsqueeze(-1) * Q).sum(1).pow(2)* sigma.unsqueeze(-1).unsqueeze(-1)).sum(0) # x, h
    T = b / (V + 1e-2)
    return T.max().clamp_min(0) * factor
  return penalty


