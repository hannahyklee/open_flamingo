from contextlib import suppress

import torch
import torch.nn as nn
from einops import rearrange
from tqdm import tqdm


def get_cast_dtype(precision: str):
    cast_dtype = None
    if precision == "bf16":
        cast_dtype = torch.bfloat16
    elif precision == "fp16":
        cast_dtype = torch.float16
    return cast_dtype


def get_autocast(precision):
    if precision == "amp":
        return torch.cuda.amp.autocast
    elif precision == "amp_bfloat16" or precision == "amp_bf16":
        # amp_bfloat16 is more stable than amp float16 for clip training
        return lambda: torch.cuda.amp.autocast(dtype=torch.bfloat16)
    else:
        return suppress


def train_one_epoch(args, model, epoch, laion_loader, pile_loader, tokenizer, optimizer, lr_scheduler, device_id, wandb, use_text_to_image_mapping=False, mapping_matrix_path=None):
    num_batches_per_epoch_laion = laion_loader.num_batches
    num_batches_per_epoch_pile = pile_loader.num_batches

    assert num_batches_per_epoch_laion == num_batches_per_epoch_pile, "Number of batches in laion and pile datasets must be the same"
    # which also = num_batches_per_epoch_pile
    num_batches_per_epoch = num_batches_per_epoch_laion

    assert mapping_matrix_path if use_text_to_image_mapping else True, "mapping_matrix_path must be provided if use_text_to_image_mapping is True"

    autocast = get_autocast(args.precision)
    cast_dtype = get_cast_dtype(args.precision)

    if use_text_to_image_mapping:
        text_to_image_transform = nn.Linear(768, 768)
        text_to_image_transform.load_state_dict(
            torch.load(mapping_matrix_path,
                       map_location=torch.device("cpu"))
        )
        text_to_image_transform.to(device_id)
        for p in text_to_image_transform.parameters():
            p.requires_grad = False

    media_token_id = tokenizer("<image>", add_special_tokens=False)[
        "input_ids"][-1]

    model.train()
    for num_steps, (batch_laion, batch_pile) in tqdm(enumerate(zip(laion_loader, pile_loader)), disable=args.rank != 0):
        global_step = num_steps + epoch * num_batches_per_epoch

        #### LAION FORWARD PASS ####
        images = batch_laion[0].to(device_id, dtype=cast_dtype,
                                   non_blocking=True).unsqueeze(1).unsqueeze(1)

        input_ids = batch_laion[1][0].to(
            device_id, dtype=cast_dtype, non_blocking=True)
        attention_mask = batch_laion[1][1].to(
            device_id, dtype=cast_dtype, non_blocking=True)

        labels = input_ids.clone()
        labels[labels == tokenizer.pad_token_id] = -100
        labels[:, 0] = -100
        labels[labels == media_token_id] = -100
        labels.to(device_id)

        with autocast():
            loss_laion = model(images, input_ids, attention_mask=attention_mask,
                               labels=labels, is_vision_encoded=False)[0]
        divided_loss_laion = loss_laion / args.gradient_accumulation_steps

        #### PILE FORWARD PASS ####
        input_ids = torch.stack([x[0] for x in batch_pile[1]]).squeeze(1)
        attention_mask = torch.stack([x[1] for x in batch_pile[1]]).squeeze(1)
        clip_text_input_ids = torch.stack(
            [x[0] for x in batch_pile[0]]).to(device_id, dtype=cast_dtype, non_blocking=True)
        clip_text_attention_mask = torch.stack(
            [x[1] for x in batch_pile[0]]).to(device_id, dtype=cast_dtype, non_blocking=True)

        N = clip_text_input_ids.shape[0]
        I = clip_text_input_ids.shape[1]
        clip_text_input_ids = rearrange(
            clip_text_input_ids, 'n h w -> (n h) w')
        clip_text_attention_mask = rearrange(
            clip_text_attention_mask, 'n h w -> (n h) w')

        with torch.no_grad():
            vision_features = model.module.vision_encoder.get_text_features(
                input_ids=clip_text_input_ids, attention_mask=clip_text_attention_mask)
            vision_features = vision_features / \
                vision_features.norm(p=2, dim=-1, keepdim=True)

        if use_text_to_image_mapping:
            with torch.no_grad():
                vision_features = text_to_image_transform(vision_features)

        # bring back the N demension
        vision_features = rearrange(
            vision_features, '(n h) w -> n h w', n=N, h=I)
        vision_features = vision_features.unsqueeze(2).unsqueeze(2)

        labels = input_ids.clone()
        labels[labels == tokenizer.pad_token_id] = -100
        labels[:, 0] = -100

        # remove loss for any token before the first <image> token
        for i in range(labels.shape[0]):
            label_idx = 0
            while label_idx < labels.shape[1] and labels[i][label_idx] != media_token_id:
                labels[i][label_idx] = -100
                label_idx += 1

        labels[labels == media_token_id] = -100
        labels.to(device_id)

        with autocast():
            loss_pile = model(vision_features, input_ids, attention_mask=attention_mask,
                              labels=labels, is_vision_encoded=True)[0]
        divided_loss_pile = loss_pile / args.gradient_accumulation_steps

        #### BACKWARD PASS ####
        loss = divided_loss_laion + divided_loss_pile
        loss.backward()

        torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)

        if (((num_steps + 1) % args.gradient_accumulation_steps) == 0) or (num_steps == num_batches_per_epoch - 1):
            optimizer.step()
            lr_scheduler.step()
            optimizer.zero_grad()

            if args.rank == 0 and args.report_to_wandb:
                wandb.log({"loss_laion": divided_loss_laion.item(),
                          "global_step": global_step}, commit=False)
                wandb.log({"loss_pile": divided_loss_pile.item(),
                          "global_step": global_step}, commit=True)


def get_checkpoint(model):
    state_dict = model.state_dict()

    for name, p in model.named_parameters():
        if not p.requires_grad:
            del state_dict[name]

    return state_dict
