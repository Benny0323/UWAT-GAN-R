import argparse
import yaml
from dataloader import VT_dataloader
from torch.utils import data
from models.models import *
from models.discriminator_models import MultiscaleDiscriminator
from utils.VTGAN_loss import *
import torch.optim as optim
from torch import nn
from utils.visualization import Visualizer
import torch
import logging
from metrics.Fid_computer import Kid_Or_Fid
from functools import partial

logging.basicConfig(filename='run.log', datefmt="%Y-%m-%d %H:%M:%S", filemode='w', level=logging.INFO,
                    format="%(asctime)s | %(message)s")


def convert_to_cuda(x, device=None):
    if device is None:
        return x.cuda()
    else:
        return x.to(device)


def load_config(file_path):
    with open(file_path, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)
        return config


def check_dir(dire):
    import os
    os.makedirs(dire, exist_ok=True)
    return dire


def main(args):
    train_config = load_config(args.model_config_path)
    BATCHSIZE = train_config['batchsize']
    EPOCHS = train_config['epoch']
    IF_RESUME = train_config['if_resume']
    weights_up_dir = train_config['save_weights_up_dir']
    check_dir(weights_up_dir)
    data_path = train_config["official_data_path"]
    img_size = tuple(train_config["img_size"])
    norm_type = train_config['norm_name']
    num_D = train_config['num_D']
    num_D_small = num_D//2
    n_layers = train_config['n_layers']
    n_layers_small = n_layers
    nlr = 0.0002
    nbeta1 = 0.5

    vgg_loss = VGGLoss()
    # if NORMAL_VIT:
    #     hinge_loss = nn.MSELoss()
    # else:
    #     hinge_loss = MyHingeLoss()
    mse = nn.MSELoss()
    l1_loss = nn.L1Loss()
    norm_layer = nn.InstanceNorm2d if norm_type is 'instance' else nn.BatchNorm2d
    gan_loss_computer = partial(Discriminator_loss_computer, loss_fn=mse, device_fn=convert_to_cuda)
    feat_loss_computer = partial(Feat_loss_computer, loss_fn=l1_loss)
    
    F_A_dataset = VT_dataloader.slo_ffa_dataset(data_path, img_size)
    F_A_dataloader = data.DataLoader(F_A_dataset, BATCHSIZE, shuffle=train_config['to_shuffle'])
    val_dataloader = data.DataLoader(F_A_dataset, 1, shuffle=train_config['to_shuffle'])
    val_iter = iter(val_dataloader)
    
    visualizer = Visualizer(weights_up_dir=check_dir(weights_up_dir+'tb_result'), way='tensorboard')

    d_model1 = nn.DataParallel(MultiscaleDiscriminator(input_nc=4, norm_layer=norm_layer, num_D=num_D, 
                                                       n_layers=n_layers, getIntermFeat=True)).cuda()
    d_model2 = nn.DataParallel(MultiscaleDiscriminator(input_nc=4, ndf=32, norm_layer=norm_layer, num_D=num_D_small, 
                                                       n_layers=n_layers_small, getIntermFeat=True)).cuda()

    # official code use separable conv2d to avoid high memory requiring, 
    # however, it doesn't take a lot if we use conv2d instead 
    if not train_config['use_separable']:
        g_model_coarse = nn.DataParallel(coarse_generator(norm_type=norm_type)).cuda()
        g_model_fine = nn.DataParallel(fine_generator(norm_type=norm_type)).cuda()
    else:
        g_model_coarse = nn.DataParallel(coarse_generator(use_separable=True, norm_type=norm_type)).cuda()
        g_model_fine = nn.DataParallel(fine_generator(use_separable=True, norm_type=norm_type)).cuda()

    if IF_RESUME:
        d_model1.module.load_state_dict(torch.load(weights_up_dir + 'd_model_1_fine.pt'))
        d_model2.module.load_state_dict(torch.load(weights_up_dir + 'd_model_2_coarse.pt'))
        g_model_fine.module.load_state_dict(torch.load(weights_up_dir + 'g_model_fine.pt'))
        g_model_coarse.module.load_state_dict(torch.load(weights_up_dir + 'g_model_coarse.pt'))

    len_along_epoch = len(F_A_dataloader)

    optimizerD_f = optim.Adam(d_model1.parameters(), lr=nlr, betas=(nbeta1, 0.999))
    optimizerD_c = optim.Adam(d_model2.parameters(), lr=nlr, betas=(nbeta1, 0.999))
    optimizerG_f = optim.Adam(g_model_fine.parameters(), lr=nlr, betas=(nbeta1, 0.999))
    optimizerG_c = optim.Adam(g_model_coarse.parameters(), lr=nlr, betas=(nbeta1, 0.999))

    metrics_computer = Kid_Or_Fid(if_cuda=False)
    count = 0
    best_fid_score = 100.
    # viz.line([[5.], [5.], [70.]], [0], win="VTGAN_LOSS", opts=dict(title='loss',
    #                                                                legend=['d_f_loss', 'd_c_loss', 'gan_loss']))
    # viz.line([[70.]], [0], win="Fid_score", opts=dict(title='Fid',
    #                                                                legend=['fid']))
    # viz.line([[0.], [0.]], [0], win="Kid_score", opts=dict(title='Kid',
    #                                                                legend=['kid_mean', 'kid_std']))
    
    visualizer.scalars_initialize()

    for epoch in range(EPOCHS):
        D_f_loss = 0
        D_c_loss = 0
        Gan_loss = 0

        for j, variable_list in enumerate(F_A_dataloader):

            variable_list = map(convert_to_cuda, variable_list)
            X_realA, X_realB, X_realA_half, X_realB_half = variable_list

            # train the FINE descriminator------------------------------------------------------------
            for _ in range(2):

                optimizerD_f.zero_grad()
                d_feat1_real = d_model1(torch.cat([X_realA, X_realB], dim=1))
                
                

                d_loss1 = gan_loss_computer(model_output=d_feat1_real, label=True)
                
                with torch.no_grad():
                    X_fakeB_half, x_global = g_model_coarse(X_realA_half)
                    X_fakeB = g_model_fine(X_realA, x_global)
                
                d_feat1_fake = d_model1(torch.cat([X_realA, X_fakeB.detach()], dim=1))


                d_loss2 = gan_loss_computer(model_output=d_feat1_fake, label=False)

                d_f_loss = d_loss1 + d_loss2
                d_f_loss.backward()
                optimizerD_f.step()

                # train the COARSE descriminator-----------------------------------------------------------------
                optimizerD_c.zero_grad()
                d_feat2_real = d_model2(torch.cat([X_realA_half, X_realB_half], dim=1))


                # d_loss3 = mse(d_feat2_real[0], y1) + ca_loss(d_feat2_real[1], y2)
                d_loss3 = gan_loss_computer(model_output=d_feat2_real, label=True)

                d_feat2_fake = d_model2(torch.cat([X_realA_half, X_fakeB_half.detach()], dim=1))


                # d_loss4 = mse(d_feat2_fake[0], y1_coarse) + ca_loss(d_feat2_fake[1], y2)
                d_loss4 = gan_loss_computer(model_output=d_feat2_fake, label=False)

                d_c_loss = d_loss3 + d_loss4
                d_c_loss.backward()
                optimizerD_c.step()

            # optimizerG_f.zero_grad()
            # optimizerG_c.zero_grad()
            # X_fakeB_half, x_global = g_model_coarse(X_realA_half)
            # X_fakeB = g_model_fine(X_realA, x_global)
            # g_f_loss = mse(X_fakeB, X_realB)

            # g_c_loss = mse(X_fakeB_half, X_realB_half)
            # g_total_loss = g_f_loss + g_c_loss

            # g_total_loss.backward()
            # optimizerG_f.step()
            # optimizerG_c.step()

            # train the FINE and COARSE together as a gan model-------------------------------------------------------------

            X_fakeB_half, x_global = g_model_coarse(X_realA_half)
            # X_fakeB = g_model_fine(X_realA, x_global.detach())
            X_fakeB = g_model_fine(X_realA, x_global)

            d_feat1_real = d_model1(torch.cat([X_realA, X_realB], dim=1))
            d_feat1_fake = d_model1(torch.cat([X_realA, X_fakeB], dim=1))

            d_feat2_real = d_model2(torch.cat([X_realA_half, X_realB_half], dim=1))
            d_feat2_fake = d_model2(torch.cat([X_realA_half, X_fakeB_half], dim=1))

            

            variable_list_stacked = X_realB, X_fakeB, X_realB_half, X_fakeB_half
            variable_list_stacked = map(lambda x: torch.cat([x, x, x], dim=1), variable_list_stacked)

            X_realB_stacked, X_fakeB_stacked, X_realB_half_stacked, X_fakeB_half_stacked = variable_list_stacked

            optimizerG_f.zero_grad()
            optimizerG_c.zero_grad()
            
            loss_G_F_GAN = gan_loss_computer(model_output=d_feat1_fake, label=True)
            loss_G_F_GAN_Feat = 10*feat_loss_computer((d_feat1_real, d_feat1_fake), num_D=num_D, 
                                                      n_layers=n_layers)
            loss_G_F_VGG = 10 * vgg_loss(X_fakeB_stacked, X_realB_stacked)
            
            loss_G_C_GAN = gan_loss_computer(model_output=d_feat2_fake, label=True)  
            loss_G_C_GAN_Feat =  10*feat_loss_computer((d_feat2_real, d_feat2_fake), num_D=num_D_small, 
                                                      n_layers=n_layers_small)
            loss_G_C_VGG = 10 * vgg_loss(X_fakeB_half_stacked, X_realB_half_stacked)
                
            gan1_loss = loss_G_F_GAN + loss_G_F_GAN_Feat + loss_G_F_VGG
            gan2_loss = loss_G_C_GAN + loss_G_C_GAN_Feat + loss_G_C_VGG
            

            gan_loss = gan1_loss + gan2_loss
            gan_loss.backward()
            optimizerG_f.step()
            optimizerG_c.step()

            with torch.no_grad():
                d_f_loss = d_loss1.item() + d_loss2.item()
                d_c_loss = d_loss3.item() + d_loss4.item()

                D_f_loss += d_f_loss
                D_c_loss += d_c_loss

                gan_loss = gan1_loss.item() + gan2_loss.item()
                Gan_loss += gan_loss

            if (j + 1) % 100 == 0:
                logging.info("\n")
                logging.info(f">{epoch+1}<1>: d_f_loss: {d_f_loss:.5f}   d_c_loss: {d_c_loss:.5f}   gan_loss: {gan_loss:.5f}")
                logging.info(f">{epoch+1}<2>: F_GAN: {loss_G_F_GAN:.5f}, F_GAN_Feat: {loss_G_F_GAN_Feat:.5f}, F_VGG: {loss_G_F_VGG:.5f}")
                logging.info(f">{epoch+1}<2>: C_GAN: {loss_G_C_GAN:.5f}, C_GAN_Feat: {loss_G_C_GAN_Feat:.5f}, C_VGG: {loss_G_C_VGG:.5f}")
            

                print()
                print(f">{epoch+1}<1>: d_f_loss: {d_f_loss:.5f}   d_c_loss: {d_c_loss:.5f}   gan_loss: {gan_loss:.5f}")
                print(f">{epoch+1}<2>: F_GAN: {loss_G_F_GAN:.5f}, F_GAN_Feat: {loss_G_F_GAN_Feat:.5f}, F_VGG: {loss_G_F_VGG:.5f}")
                print(f">{epoch+1}<2>: C_GAN: {loss_G_C_GAN:.5f}, C_GAN_Feat: {loss_G_C_GAN_Feat:.5f}, C_VGG: {loss_G_C_VGG:.5f}")
                print()

                



        D_f_loss /= len_along_epoch
        D_c_loss /= len_along_epoch
        Gan_loss /= len_along_epoch

        logging.info(
            ">>>>%d: d_f_loss: %5f   d_c_loss: %5f   gan_loss: %5f" % (epoch + 1, D_f_loss, D_c_loss, Gan_loss))
        print(">>>>%d: d_f_loss: %5f   d_c_loss: %5f   gan_loss: %5f" % (epoch + 1, D_f_loss, D_c_loss, Gan_loss))
        visualizer.iter_summarize_performance(g_model_fine, g_model_coarse, val_iter, str(epoch + 1))
        count += len_along_epoch
        metrics_computer.update_models(g_fine_model=g_model_fine, g_coarse_model=g_model_coarse)
        fid_score, kid_mean, kid_std = metrics_computer.spin_once()
        # viz.line([[fid_score]], [epoch+1], win='Fid_score', update='append')
        # viz.line([[kid_mean], [kid_std]], [epoch+1], win='Kid_score', update='append')
        visualizer.scalar_adjuster([fid_score], epoch+1, 'Fid_score', ['fid_score'])
        visualizer.scalar_adjuster([kid_mean, kid_std], epoch+1, 'Kid_score', legend=['kid_mean', 'kid_std'])
        # viz.line([[D_f_loss], [D_c_loss], [Gan_loss]], [epoch + 1], win="VTGAN_LOSS", update="append")
        visualizer.scalar_adjuster([D_f_loss*10, D_c_loss*10, Gan_loss], epoch+1, "VTGAN_LOSS", 
                                   legend=['df_loss', 'dc_loss', 'gan_loss'])

        if fid_score < best_fid_score:
            best_fid_score = fid_score
            torch.save(g_model_coarse.module.state_dict(), weights_up_dir + 'g_model_coarse.pt')
            torch.save(g_model_fine.module.state_dict(), weights_up_dir + 'g_model_fine.pt')
            torch.save(d_model1.module.state_dict(), weights_up_dir + 'd_model_1_fine.pt')
            torch.save(d_model2.module.state_dict(), weights_up_dir + 'd_model_2_coarse.pt')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--model_config_path', type=str, default='config/common_discriminator.yaml')

    args = parser.parse_args()
    main(args)
