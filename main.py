"""Publish the one word to erase from the dictionary to entry.txt.

The entry is a single word, "bumfuzzle", checked against the sealed
gate (UTF-8 text, 1-77 characters, not only whitespace, exactly one
word, nothing invisible) and written atomically, so the output
directory may be a bind mount and a crashed run can never leave a
half-written file behind. README.md records the losing alternatives
and why they lost.
"""

import os
import stat
import sys
import unicodedata
from contextlib import suppress

# The word itself. One word, 9 characters, plain visible text.
# "Bumfuzzle" is a real dictionary verb (US dialect): to confuse or
# perplex. It sounds like bewildered mumbling, so the joke is in the
# word itself, and deleting it would leave the language with no word
# for the very confusion the word describes.
ENTRY = "bumfuzzle"

LIMIT_CHARS = 77
OUTPUT_NAME = "entry.txt"
STAGING_PREFIX = ".entry-"
STAGING_SUFFIX = ".tmp"
BREAK_CHARS = "\n\r\u2028\u2029"

# Code points refused no matter what their category claims.
NONCHARACTERS = frozenset(range(0xFDD0, 0xFDF0)) | {0xFFFE, 0xFFFF}
C1_RANGE = range(0x0080, 0x00A0)
SURROGATE_RANGE = range(0xD800, 0xE000)


def _hex_reason(template):
    """Build a reason function that formats a code point into a template."""
    return lambda code: template % code


# One rule per forbidden character kind, in refusal order: the first
# rule that matches names the problem. Each rule is a predicate over
# (character, code point, Unicode category) plus a fixed reason or a
# reason function of the code point.
_CHAR_RULES = (
    (lambda ch, code, cat: ch in BREAK_CHARS,
     "line break"),
    (lambda ch, code, cat: ch == " ",
     "space: the entry must be exactly one word"),
    (lambda ch, code, cat: code == 0xFEFF,
     "byte order mark (U+FEFF)"),
    (lambda ch, code, cat: code in NONCHARACTERS,
     _hex_reason("Unicode noncharacter U+%04X")),
    (lambda ch, code, cat: code in SURROGATE_RANGE,
     _hex_reason("surrogate U+%04X")),
    (lambda ch, code, cat: code in C1_RANGE or cat == "Cc",
     _hex_reason("control character U+%04X")),
    (lambda ch, code, cat: cat == "Cf",
     _hex_reason("invisible format character U+%04X")),
    (lambda ch, code, cat: cat == "Zs",
     _hex_reason("Unicode space U+%04X")),
    (lambda ch, code, cat: cat in ("Zl", "Zp"),
     _hex_reason("line separator U+%04X")),
)


def _reason_for_char(ch):
    """Name the problem with one character, or None when it is fine.

    Anything that could hide, break the word, or smuggle bytes past
    the gate is refused: line breaks, spaces of any kind, the byte
    order mark, Unicode noncharacters, surrogates, controls,
    invisible format marks, and Unicode spaces.
    """
    code = ord(ch)
    category = unicodedata.category(ch)
    for predicate, reason in _CHAR_RULES:
        if predicate(ch, code, category):
            return reason(code) if callable(reason) else reason
    return None


def _shape_message(text):
    """Name the first shape problem with the text, or None when fine."""
    for check, message in _SHAPE_RULES:
        if check(text):
            return message(text) if callable(message) else message
    return None


# One rule per shape failure, in refusal order: the first matching
# rule names the problem and nothing later runs. Shape is about
# length and whitespace; wordness lives in _validate_word, and
# forbidden characters in _validate_characters.
_SHAPE_RULES = (
    (lambda text: not isinstance(text, str),
     "entry must be a string"),
    (lambda text: not text,
     "entry is empty"),
    (lambda text: not text.strip(),
     "entry is only whitespace"),
    (lambda text: len(text) > LIMIT_CHARS,
     lambda text: "entry is over the %d limit" % LIMIT_CHARS),
    (lambda text: text != text.strip(),
     "entry has leading or trailing whitespace"),
    (lambda text: any(ch in BREAK_CHARS for ch in text),
     "entry contains a line break"),
)


def _validate_shape(text):
    """Refuse text whose overall shape breaks the gate."""
    message = _shape_message(text)
    if message is not None:
        raise ValueError(message)


def _validate_word(text):
    """Refuse text that is not exactly one word of letters.

    A space anywhere, a digit, a hyphen, an apostrophe, or any other
    punctuation would make the entry more than the one word the
    judge is asked to erase, so the whole entry is refused.
    """
    if " " in text:
        raise ValueError("entry must be exactly one word")
    if not text.isalpha():
        raise ValueError("entry must be letters only")


def _validate_characters(text):
    """Refuse text holding even one forbidden character.

    The whole entry is rejected, never repaired: the first bad
    character found names the refusal.
    """
    for ch in text:
        reason = _reason_for_char(ch)
        if reason is not None:
            raise ValueError("entry contains a forbidden character: %s" % reason)


def validate_entry(text):
    """Run every gate check; hand the text back unchanged when it passes."""
    _validate_shape(text)
    _validate_word(text)
    _validate_characters(text)
    return text


def _read_output_dir_setting():
    """Read MAKEFUN_OUTPUT_DIR (default /output) and refuse bad values."""
    raw = os.environ.get("MAKEFUN_OUTPUT_DIR", "/output")
    if not raw:
        raise ValueError("MAKEFUN_OUTPUT_DIR is empty")
    if "\x00" in raw:
        raise ValueError("MAKEFUN_OUTPUT_DIR contains a NUL byte")
    return raw


def _resolve_output_dir():
    """Turn the output-directory setting into an absolute path."""
    return os.path.abspath(_read_output_dir_setting().strip())


def _ensure_writable_directory(path):
    """Create the output directory when missing; refuse it when unusable."""
    if os.path.lexists(path) and not os.path.isdir(path):
        raise ValueError("output path %r is not a directory" % path)
    os.makedirs(path, exist_ok=True)
    if not os.access(path, os.W_OK):
        raise ValueError("output directory %r is not writable" % path)
    return path


def prepare_output_directory():
    """Resolve the output directory and make sure it is usable.

    The directory is created when missing and must be a real,
    writable directory. Nothing inside it is touched here; clearing
    happens in the next step, once the entry itself has validated.
    """
    return _ensure_writable_directory(_resolve_output_dir())


def _unlink_quietly(path):
    """Unlink one file or symlink; ignore it when already gone."""
    with suppress(FileNotFoundError, OSError):
        os.unlink(path)


def clear_output_contents(path):
    """Empty the output directory's contents, never the directory.

    Plain files and symlinks go; subdirectories are left alone. This
    runs before publishing so a stale entry.txt or staging file from
    an earlier run can never be mistaken for this run's output.
    """
    with os.scandir(path) as entries:
        for entry in entries:
            if entry.is_symlink() or entry.is_file(follow_symlinks=False):
                _unlink_quietly(entry.path)


def _sync_directory(directory):
    """Fsync the directory so a rename inside it is durable."""
    try:
        with os.fdopen(os.open(directory, os.O_RDONLY)) as handle:
            os.fsync(handle.fileno())
    except OSError:
        pass


def atomic_write(directory, payload):
    """Publish payload bytes as entry.txt inside directory, atomically.

    The payload is staged in a temporary file that lives inside the
    output directory, so the rename never crosses a filesystem (a
    bind mount is safe). A stale staging file with the same name is
    discarded first; the exclusive create then cannot collide with a
    previous run that died mid-publish. The staged bytes are flushed
    to disk, the file is forced to mode 0644 whatever the umask was,
    and os.replace moves it over entry.txt. A failed rename cleans
    up the staging file and the error propagates.
    """
    tmp_name = "%s%d%s" % (STAGING_PREFIX, os.getpid(), STAGING_SUFFIX)
    tmp_path = os.path.join(directory, tmp_name)
    _unlink_quietly(tmp_path)
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(fd, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(tmp_path, 0o644)
    final_path = os.path.join(directory, OUTPUT_NAME)
    try:
        os.replace(tmp_path, final_path)
    except BaseException:
        _unlink_quietly(tmp_path)
        raise
    _sync_directory(directory)
    return final_path


def _check_published_mode(path):
    """Refuse a published file whose mode is not 0644."""
    if stat.S_IMODE(os.stat(path).st_mode) != 0o644:
        raise ValueError("published file mode is not 0644")


def _verify_published(path, payload):
    """Read the published file back and confirm bytes, text and mode."""
    with open(path, "rb") as handle:
        data = handle.read()
    if data != payload:
        raise ValueError("published file differs from the payload")
    text = data.decode("utf-8")
    validate_entry(text.rstrip("\n"))
    _check_published_mode(path)
    return text


def publish_entry():
    """Validate the entry, publish it atomically, and verify the result.

    Returns the published text. Every failure raises; nothing is
    printed here, so a caller decides how a failure is reported.
    """
    payload = (validate_entry(ENTRY) + "\n").encode("utf-8")
    directory = prepare_output_directory()
    clear_output_contents(directory)
    return _verify_published(atomic_write(directory, payload), payload)


def main(argv=None):
    """Publish the entry; report any failure on stderr and exit 1."""
    try:
        publish_entry()
    except (ValueError, OSError, UnicodeError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
