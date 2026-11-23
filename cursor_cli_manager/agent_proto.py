"""Read text from Cursor's Protobuf checkpoint and its SQLite blobs."""
from __future__ import annotations

from typing import Callable, Iterator, List, Optional, Sequence, Tuple


def _varint(data: bytes, offset: int) -> Tuple[int, int]:
    value = 0
    for shift in range(0, 64, 7):
        if offset >= len(data):
            break
        byte = data[offset]
        offset += 1
        value |= (byte & 127) << shift
        if byte < 128:
            return value, offset
    raise ValueError("invalid Protobuf varint")


def _bytes_fields(data: bytes, field: int) -> List[bytes]:
    values = []
    offset = 0
    while offset < len(data):
        tag, offset = _varint(data, offset)
        number, wire = divmod(tag, 8)
        if not number:
            raise ValueError("invalid Protobuf field number")
        if wire == 0:
            _, offset = _varint(data, offset)
            continue
        if wire == 2:
            size, offset = _varint(data, offset)
        elif wire == 1:
            size = 8
        elif wire == 5:
            size = 4
        else:
            raise ValueError("unknown Protobuf wire type")
        end = offset + size
        if end > len(data):
            raise ValueError("truncated Protobuf field")
        if number == field and wire == 2:
            values.append(data[offset:end])
        offset = end
    return values


def _text(data: bytes) -> str:
    values = _bytes_fields(data, 1)
    return values[-1].decode("utf-8").strip() if values else ""


def checkpoint_messages(
    root: bytes,
    read_blob: Callable[[str], Optional[bytes]],
    *,
    roles: Sequence[str],
    reverse: bool,
    max_messages: Optional[int],
    max_blobs: Optional[int],
) -> Optional[List[Tuple[str, str]]]:
    # ConversationStateStructure.turns contains SHA-256 blob IDs in display order.
    try:
        turns = _bytes_fields(root, 8)
    except ValueError:
        return None
    if not turns or any(len(ref) != 32 for ref in turns):
        return None

    remaining = max_blobs

    def load(ref: bytes) -> bytes:
        nonlocal remaining
        if len(ref) != 32 or remaining == 0:
            return b""
        if remaining is not None:
            remaining -= 1
        return read_blob(ref.hex()) or b""

    def messages(turn: bytes) -> Iterator[Tuple[str, str]]:
        # ConversationTurnStructure.agent_conversation_turn contains a user
        # message reference (1) and ordered ConversationStep references (2).
        user = _bytes_fields(turn, 1)
        refs = [("user", user[-1])] if user and "user" in roles else []
        if "assistant" in roles:
            refs.extend(("assistant", ref) for ref in _bytes_fields(turn, 2))
        for role, ref in reversed(refs) if reverse else refs:
            try:
                data = load(ref)
                if role == "assistant":
                    # ConversationStep.assistant_message is a nested message.
                    assistant = _bytes_fields(data, 1)
                    data = assistant[-1] if assistant else b""
                text = _text(data)
                if not text and role == "user":
                    text_refs = _bytes_fields(data, 18)
                    if text_refs:
                        text = load(text_refs[-1]).decode("utf-8").strip()
            except ValueError:
                continue
            if text and (role != "user" or not text.startswith("<user_info>")):
                yield role, text

    result: List[Tuple[str, str]] = []
    recognized = False
    for ref in reversed(turns) if reverse else turns:
        if remaining == 0:
            break
        try:
            data = load(ref)
            turn = _bytes_fields(data, 1)
            recognized = recognized or bool(turn or _bytes_fields(data, 2))
            for message in messages(turn[-1]) if turn else ():
                result.append(message)
                if max_messages is not None and len(result) >= max_messages:
                    return result[::-1] if reverse else result
        except ValueError:
            continue
    if not recognized:
        return None
    return result[::-1] if reverse else result
