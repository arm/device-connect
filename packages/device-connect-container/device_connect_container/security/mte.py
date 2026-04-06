"""Arm Memory Tagging Extension (MTE) enablement for containerized processes.

MTE (Armv8.5-A / Armv9) assigns a 4-bit tag to every 16-byte memory granule
and a corresponding tag to pointers. On access, hardware compares tags;
mismatches trigger a fault (SIGSEGV). This catches use-after-free and
buffer overflows in C/C++ code at near-zero performance cost.

MTE works inside containers without special Docker flags — it's a
per-process opt-in via prctl(). The host kernel must have CONFIG_ARM64_MTE=y.

Available hardware (as of early 2026):
    - AmpereOne (datacenter)
    - Google Pixel 8+ (mobile)
    - Apple A19/iPhone 17 (marketed as "Memory Integrity Enforcement")

To enable:
    1. Set MTE_ENABLED=true in container environment
    2. Compile C extensions with: -fsanitize=memtag (Clang 12+)
    3. The sidecar_runtime calls enable_mte_for_process() at startup

Modes:
    - SYNC: Synchronous tag check — immediate SIGSEGV on mismatch.
      Precise but slower (~1-5% overhead). Best for development/testing.
    - ASYNC: Asynchronous tag check — queued faults, faster, less precise.
      Better for production (~0.5% overhead).
"""

import ctypes
import logging
import os
import platform
from enum import IntEnum

logger = logging.getLogger(__name__)

# prctl constants for MTE
PR_SET_TAGGED_ADDR_CTRL = 55
PR_GET_TAGGED_ADDR_CTRL = 56
PR_TAGGED_ADDR_ENABLE = (1 << 0)
PR_MTE_TCF_NONE = 0
PR_MTE_TCF_SYNC = (1 << 1)
PR_MTE_TCF_ASYNC = (1 << 2)
# Tag mask: allow all tag values except 0 (0 = untagged, reserved)
PR_MTE_TAG_MASK = 0xFFFE << 3  # bits [20:3] = tag mask


class MteMode(IntEnum):
    """MTE checking mode."""

    NONE = PR_MTE_TCF_NONE    # MTE disabled
    SYNC = PR_MTE_TCF_SYNC    # Synchronous (precise, slower)
    ASYNC = PR_MTE_TCF_ASYNC  # Asynchronous (faster, imprecise)


def is_mte_available() -> bool:
    """Check if MTE is available on this platform.

    Returns:
        True if the CPU supports MTE and the kernel has it enabled.
    """
    if platform.machine().lower() not in ("aarch64", "arm64"):
        return False

    # Check HWCAP2 for MTE support
    try:
        # Read /proc/cpuinfo for "mte" in features
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("Features"):
                    features = line.split(":")[-1].strip().split()
                    return "mte" in features
    except FileNotFoundError:
        pass

    # Alternative: check HWCAP2 via getauxval
    try:
        libc = ctypes.CDLL("libc.so.6")
        AT_HWCAP2 = 26
        HWCAP2_MTE = (1 << 18)
        hwcap2 = libc.getauxval(AT_HWCAP2)
        return bool(hwcap2 & HWCAP2_MTE)
    except (OSError, AttributeError):
        pass

    return False


def enable_mte_for_process(mode: MteMode = MteMode.SYNC) -> bool:
    """Enable MTE for the current process via prctl().

    This enables hardware memory tagging for all subsequent memory
    allocations in this process. Use-after-free and buffer overflows
    in C/C++ code will trigger SIGSEGV.

    Works inside containers without any special Docker flags — MTE is
    a userspace feature exposed via prctl() and mmap().

    Args:
        mode: MTE checking mode (SYNC for development, ASYNC for production).

    Returns:
        True if MTE was enabled successfully.
    """
    if not is_mte_available():
        logger.info("MTE not available on this platform")
        return False

    try:
        libc = ctypes.CDLL("libc.so.6")

        flags = PR_TAGGED_ADDR_ENABLE | mode.value | PR_MTE_TAG_MASK
        result = libc.prctl(PR_SET_TAGGED_ADDR_CTRL, flags, 0, 0, 0)

        if result == 0:
            mode_name = "SYNC" if mode == MteMode.SYNC else "ASYNC"
            logger.info("MTE enabled in %s mode", mode_name)
            return True
        else:
            logger.warning("prctl(PR_SET_TAGGED_ADDR_CTRL) returned %d", result)
            return False

    except OSError as e:
        logger.warning("Failed to enable MTE: %s", e)
        return False


def get_mte_status() -> dict:
    """Get current MTE status for this process.

    Returns:
        Dict with MTE state information.
    """
    status = {
        "available": is_mte_available(),
        "enabled": False,
        "mode": "none",
        "platform": platform.machine(),
    }

    if not status["available"]:
        return status

    try:
        libc = ctypes.CDLL("libc.so.6")
        ctrl = libc.prctl(PR_GET_TAGGED_ADDR_CTRL, 0, 0, 0, 0)

        if ctrl >= 0:
            status["enabled"] = bool(ctrl & PR_TAGGED_ADDR_ENABLE)
            if ctrl & PR_MTE_TCF_SYNC:
                status["mode"] = "sync"
            elif ctrl & PR_MTE_TCF_ASYNC:
                status["mode"] = "async"
    except OSError:
        pass

    return status
