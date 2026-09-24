"""Extract the plain NSString from Messages' attributedBody typedstream.

Only the length-delimited body is decoded. Formatting attributes are not text
and must never become commands. Unknown/corrupt archives fail closed.
"""

from __future__ import annotations


MAX_BODY_BYTES = 1_048_576


def decode_attributed_body(value: object) -> str | None:
    """Read the first attributed-string body without deserializing objects.

    Apple typedstream stores an NSString followed by a shared '+' type marker
    and a byte-counted UTF-8 string. Counts use literal bytes through 127, then
    0x81/uint16 or 0x82/uint32 in the archive's byte order. Refuse unsupported
    forms instead of scanning metadata for something that looks like prose.
    """
    if not isinstance(value, (bytes, bytearray, memoryview)):
        return None
    if not value or len(value) > MAX_BODY_BYTES:
        return None
    blob = bytes(value)
    if blob.startswith(b"\x04\x0bstreamtyped"):
        byteorder = "little"
    elif blob.startswith(b"\x04\x0btypedstream"):
        byteorder = "big"
    else:
        return None
    # The body precedes attribute dictionaries. Bound the header search so a
    # malformed blob cannot make an attribute value into an inbound message.
    header = blob[:256]
    attributed = header.find(b"AttributedString")
    string_class = header.find(b"NSString", attributed + 16)
    if attributed < 0 or string_class < 0:
        return None
    marker = header.find(b"\x01+", string_class + len(b"NSString"))
    if marker < 0 or marker - string_class > 32:
        return None
    offset = marker + 2
    if offset >= len(blob):
        return None
    tag = blob[offset]
    offset += 1
    if tag <= 0x7F:
        length = tag
    elif tag in (0x81, 0x82):
        width = 2 if tag == 0x81 else 4
        if offset + width > len(blob):
            return None
        length = int.from_bytes(blob[offset:offset + width], byteorder)
        offset += width
    else:
        return None
    end = offset + length
    if not length or end >= len(blob) or blob[end] != 0x86:
        return None
    try:
        text = blob[offset:end].decode("utf-8")
    except UnicodeDecodeError:
        return None
    # Preserve line breaks, emoji, punctuation and attachment placeholders;
    # never expose archive control bytes to the command dispatcher.
    if any(ord(char) < 32 and char not in "\r\n\t" for char in text):
        return None
    return text
