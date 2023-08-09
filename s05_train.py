import os
import json
import time
import torch.optim
import torch.utils.data
import torch.backends.cudnn as cudnn
from utils import *
from datasets import ChessDataset
from torch.cuda.amp import GradScaler
from torch.utils.data import DataLoader
from model import ChessTransformer, LabelSmoothedCE


DEVICE = torch.device(
    "cuda" if torch.cuda.is_available() else "cpu"
)  # CPU isn't really practical here

# Data parameters
data_folder = "/media/sgr/SSD/lichess data (copy)/"  # folder with data files
h5_file = "data.h5"  # HDF5 file with (encoded) data
splits_file = "splits.json"  # JSON file with split indices
vocab_file = "vocabulary.json"  # JSON file with all vocabularies

# Model parameters
d_model = 512  # size of vectors throughout the transformer model
n_heads = 8  # number of heads in the multi-head attention
d_queries = 64  # size of query vectors (and also the size of the key vectors) in the multi-head attention
d_values = 64  # size of value vectors in the multi-head attention
d_inner = 2048  # an intermediate size in the position-wise FC
n_layers = 6  # number of layers in the Encoder and Decoder
dropout = 0.1  # dropout probability
max_move_sequence_length = 10  # expected maximum length of move sequences

# Learning parameters
checkpoint = None  # path to model checkpoint, None if none
batch_size = 384
batches_per_step = (
    2500 // batch_size
)  # perform a training step, i.e. update parameters, once every so many batches
print_frequency = 1  # print status once every so many steps
n_steps = 100000  # number of training steps
warmup_steps = 8000  # number of warmup steps where learning rate is increased linearly; twice the value in the paper, as in the official transformer repo.
step = 1  # the step number, start from 1 to prevent math error in the next line
lr = get_lr(
    step=step, d_model=d_model, warmup_steps=warmup_steps
)  # see utils.py for learning rate schedule; twice the schedule in the paper, as in the official transformer repo.
start_epoch = 0  # start at this epoch
betas = (0.9, 0.98)  # beta coefficients in the Adam optimizer
epsilon = 1e-9  # epsilon term in the Adam optimizer
label_smoothing = 0.1  # label smoothing co-efficient in the Cross Entropy loss
cudnn.benchmark = False  # since input tensor size is variable
use_amp = True  # use automatic mixed precision training?


def main():
    """
    Training and validation.
    """
    global checkpoint, step, start_epoch, epoch, epochs

    # Initialize data-loaders
    train_loader = DataLoader(
        dataset=ChessDataset(
            data_folder=data_folder,
            h5_file=h5_file,
            splits_file=splits_file,
            split="train",
        ),
        batch_size=batch_size,
        num_workers=8,
        pin_memory=False,
        prefetch_factor=2,
        shuffle=True,
    )
    val_loader = DataLoader(
        dataset=ChessDataset(
            data_folder=data_folder,
            h5_file=h5_file,
            splits_file=splits_file,
            split="val",
        ),
        batch_size=batch_size,
        num_workers=8,
        pin_memory=False,
        prefetch_factor=2,
        shuffle=False,
    )

    # Initialize model or load checkpoint
    vocabulary = json.load(open(os.path.join(data_folder, vocab_file), "r"))
    vocab_sizes = dict()
    for k in vocabulary:
        vocab_sizes[k] = len(vocabulary[k])
    model = ChessTransformer(
        vocab_sizes=vocab_sizes,
        max_move_sequence_length=max_move_sequence_length,
        d_model=d_model,
        n_heads=n_heads,
        d_queries=d_queries,
        d_values=d_values,
        d_inner=d_inner,
        n_layers=n_layers,
        dropout=dropout,
    )
    optimizer = torch.optim.Adam(
        params=[p for p in model.parameters() if p.requires_grad],
        lr=lr,
        betas=betas,
        eps=epsilon,
    )
    if checkpoint is not None:
        checkpoint = torch.load(checkpoint)
        start_epoch = checkpoint["epoch"] + 1
        model.load_state_dict(checkpoint["model_state_dict"])
        optimizer.load_state_dict(checkpoint["optimizer_state_dict"])
        print("\nLoaded checkpoint from epoch %d.\n" % start_epoch)

    # Loss function
    criterion = LabelSmoothedCE(eps=label_smoothing)

    # Move to default device
    model = model.to(DEVICE)
    criterion = criterion.to(DEVICE)

    # Compile model
    compiled_model = torch.compile(model, mode="default")

    # AMP scaler
    scaler = GradScaler(enabled=use_amp)

    # Find total epochs to train
    epochs = (n_steps // (len(train_loader) // batches_per_step)) + 1

    # Epochs
    for epoch in range(start_epoch, epochs):
        # Step
        step = epoch * len(train_loader) // batches_per_step

        # One epoch's training
        train(
            train_loader=train_loader,
            model=compiled_model,
            criterion=criterion,
            optimizer=optimizer,
            scaler=scaler,
            epoch=epoch,
            step=step,
        )

        # One epoch's validation
        validate(val_loader=val_loader, model=compiled_model, criterion=criterion)

        # Save checkpoint
        save_checkpoint(epoch, compiled_model, optimizer)


def train(train_loader, model, criterion, optimizer, scaler, epoch, step):
    """
    One epoch's training.

    Args:

        train_loader (torch.utils.data.DataLoader): loader for training data

        model (torch.nn.Module): model

        criterion (torch.nn.Module): label-smoothed cross-entropy loss

        optimizer (torch.optim.adam.Adam): optimizer

        scaler (torch.cuda.amp.GradScaler): AMP scaler

        epoch (int): epoch number

        step (int): step number
    """
    model.train()  # training mode enables dropout

    # Track some metrics
    data_time = AverageMeter()  # data loading time
    step_time = AverageMeter()  # forward prop. + back prop. time
    losses = AverageMeter()  # loss

    # Starting time
    start_data_time = time.time()
    start_step_time = time.time()

    # Batches
    for i, (
        turns,
        white_kingside_castling_rights,
        white_queenside_castling_rights,
        black_kingside_castling_rights,
        black_queenside_castling_rights,
        can_claim_draw,
        board_positions,
        moves,
        lengths,
    ) in enumerate(train_loader):

        # Move to default device
        turns = turns.to(DEVICE)  # (N, 1)
        white_kingside_castling_rights = white_kingside_castling_rights.to(
            DEVICE
        )  # (N, 1)
        white_queenside_castling_rights = white_queenside_castling_rights.to(
            DEVICE
        )  # (N, 1)
        black_kingside_castling_rights = black_kingside_castling_rights.to(
            DEVICE
        )  # (N, 1)
        black_queenside_castling_rights = black_queenside_castling_rights.to(
            DEVICE
        )  # (N, 1)
        can_claim_draw = can_claim_draw.to(DEVICE)  # (N, 1)
        board_positions = board_positions.to(DEVICE)  # (N, 64)
        moves = moves.to(DEVICE)  # (N, max_move_sequence_length + 1)
        lengths = lengths.squeeze(1).to(DEVICE)  # (N, 1)

        # Time taken to load data
        data_time.update(time.time() - start_data_time)

        with torch.autocast(
            device_type=DEVICE.type, dtype=torch.float16, enabled=use_amp
        ):
            # Forward prop.
            # Note: If "max_move_sequence_length" is 8
            # then the move sequence will be like "<move> a b c <loss> <pad> <pad> <pad> <pad>"
            # We do not pass the last token to the Decoder as input (i.e. we left shift)
            predicted_moves = model(
                turns=turns,
                white_kingside_castling_rights=white_kingside_castling_rights,
                white_queenside_castling_rights=white_queenside_castling_rights,
                black_kingside_castling_rights=black_kingside_castling_rights,
                black_queenside_castling_rights=black_queenside_castling_rights,
                can_claim_draw=can_claim_draw,
                board_positions=board_positions,
                moves=moves[:, :-1],
                lengths=lengths,
            )  # (N, max_move_sequence_length, move_vocab_size)

            # Loss
            # Note: If max_move_sequence_length is 8
            # then the move sequence will be like "<move> a b c <loss> <pad> <pad> <pad> <pad>"
            # We do not pass the first token as an "actual_move" as it is not one (i.e. we right shift)
            loss = criterion(
                moves=predicted_moves, actual_moves=moves[:, 1:], lengths=lengths
            )  # scalar
            loss = loss / batches_per_step

        # Backward prop.
        scaler.scale(loss).backward()

        # Keep track of losses
        losses.update(loss.item() * batches_per_step, lengths.sum().item())

        # Update model (i.e. perform a training step) only after gradients are accumulated from batches_per_step batches
        if (i + 1) % batches_per_step == 0:
            scaler.step(optimizer)
            scaler.update()
            # optimizer.step()
            optimizer.zero_grad()

            # This step is now complete
            step += 1

            # Update learning rate after each step
            change_lr(
                optimizer,
                new_lr=get_lr(step=step, d_model=d_model, warmup_steps=warmup_steps),
            )

            # Time taken for this training step
            step_time.update(time.time() - start_step_time)

            # Print status
            if step % print_frequency == 0:
                print(
                    "Epoch {0}/{1}-----"
                    "Batch {2}/{3}-----"
                    "Step {4}/{5}-----"
                    "Data Time {data_time.val:.3f} ({data_time.avg:.3f})-----"
                    "Step Time {step_time.val:.3f} ({step_time.avg:.3f})-----"
                    "Loss {losses.val:.4f} ({losses.avg:.4f})".format(
                        epoch + 1,
                        epochs,
                        i + 1,
                        len(train_loader),
                        step,
                        n_steps,
                        step_time=step_time,
                        data_time=data_time,
                        losses=losses,
                    )
                )

            # Reset step time
            start_step_time = time.time()

            # If this is the last one or two epochs, save checkpoints at regular intervals for averaging
            if (
                epoch in [epochs - 1, epochs - 2] and step % 1500 == 0
            ):  # 'epoch' is 0-indexed
                save_checkpoint(
                    epoch, model, optimizer, prefix="step" + str(step) + "_"
                )

        # Reset data time
        start_data_time = time.time()


def validate(val_loader, model, criterion):
    """
    One epoch's validation.

    Args:

        val_loader (torch.utils.data.DataLoader): loader for validation data

        model (torch.nn.Module): model

        criterion (torch.nn.Module): label-smoothed cross-entropy loss
    """
    model.eval()  # eval mode disables dropout

    # Prohibit gradient computation explicitly
    with torch.no_grad():
        losses = AverageMeter()
        # Batches
        for i, (
            turns,
            white_kingside_castling_rights,
            white_queenside_castling_rights,
            black_kingside_castling_rights,
            black_queenside_castling_rights,
            can_claim_draw,
            board_positions,
            moves,
            lengths,
        ) in enumerate(val_loader):

            # Move to default device
            turns = turns.to(DEVICE)  # (N, 1)
            white_kingside_castling_rights = white_kingside_castling_rights.to(
                DEVICE
            )  # (N, 1)
            white_queenside_castling_rights = white_queenside_castling_rights.to(
                DEVICE
            )  # (N, 1)
            black_kingside_castling_rights = black_kingside_castling_rights.to(
                DEVICE
            )  # (N, 1)
            black_queenside_castling_rights = black_queenside_castling_rights.to(
                DEVICE
            )  # (N, 1)
            can_claim_draw = can_claim_draw.to(DEVICE)  # (N, 1)
            board_positions = board_positions.to(DEVICE)  # (N, 64)
            moves = moves.to(DEVICE)  # (N, max_move_sequence_length + 1)
            lengths = lengths.squeeze(1).to(DEVICE)  # (N, 1)

            with torch.autocast(
                device_type=DEVICE.type, dtype=torch.float16, enabled=use_amp
            ):
                # Forward prop.
                # Note: If "max_move_sequence_length" is 8
                # then the move sequence will be like "<move> a b c <loss> <pad> <pad> <pad> <pad>"
                # We do not pass the last token to the Decoder as input (i.e. we left shift)
                predicted_moves = model(
                    turns=turns,
                    white_kingside_castling_rights=white_kingside_castling_rights,
                    white_queenside_castling_rights=white_queenside_castling_rights,
                    black_kingside_castling_rights=black_kingside_castling_rights,
                    black_queenside_castling_rights=black_queenside_castling_rights,
                    can_claim_draw=can_claim_draw,
                    board_positions=board_positions,
                    moves=moves[:, :-1],
                    lengths=lengths,
                )  # (N, max_move_sequence_length, move_vocab_size)

                # Loss
                # Note: If max_move_sequence_length is 8
                # then the move sequence will be like "<move> a b c <loss> <pad> <pad> <pad> <pad>"
                # We do not pass the first token as an "actual_move" as it is not one (i.e. we right shift)
                loss = criterion(
                    moves=predicted_moves, actual_moves=moves[:, 1:], lengths=lengths
                )  # scalar

            # Keep track of losses
            losses.update(loss.item(), lengths.sum().item())

        print("\nValidation loss: %.3f\n\n" % losses.avg)


if __name__ == "__main__":
    main()