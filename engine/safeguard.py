# -*- coding: utf-8 -*-
"""安全护栏: 拒绝把输出写到源盘上
照SectorForge drives/windows.rs的映射链用ctypes实现:
路径 → GetVolumePathNameW挂载点 → GetDriveTypeW过滤网络/RAM盘
→ GetVolumeNameForVolumeMountPointW卷GUID → 去尾反斜杠打开卷(零权限)
→ IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS → 物理盘号
fail-closed: 判不出输出所在盘也拒绝。
"""
import os
import ctypes
from ctypes import wintypes

k32 = ctypes.windll.kernel32

# 显式签名: 不声明会把HANDLE截断成32位、指针错位
k32.CreateFileW.restype = wintypes.HANDLE
k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                            ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
k32.CloseHandle.argtypes = [wintypes.HANDLE]
k32.CloseHandle.restype = wintypes.BOOL
k32.DeviceIoControl.restype = wintypes.BOOL
k32.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p,
                                wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
k32.GetVolumePathNameW.restype = wintypes.BOOL
k32.GetVolumePathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
k32.GetDriveTypeW.restype = wintypes.UINT
k32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
k32.GetVolumeNameForVolumeMountPointW.restype = wintypes.BOOL
k32.GetVolumeNameForVolumeMountPointW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]

GENERIC_READ = 0x80000000
FILE_SHARE_READ = 1
FILE_SHARE_WRITE = 2
OPEN_EXISTING = 3
DRIVE_REMOTE = 4
DRIVE_RAMDISK = 6
IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS = 0x00560000
INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value


class OutputOnSourceDrive(Exception):
    pass


def _physical_drive_of_source(source):
    r"""源是 \\.\PhysicalDriveN → N; 其他形式尽力解析, 失败返回None"""
    import re
    m = re.search(r'physicaldrive(\d+)\s*$', source.replace('/', '\\').lower())
    return int(m.group(1)) if m else None


def _open_volume_zero_access(volume_name):
    """零权限打开卷: 足够发IOCTL且无需管理员"""
    h = k32.CreateFileW(volume_name, 0, FILE_SHARE_READ | FILE_SHARE_WRITE,
                        None, OPEN_EXISTING, 0, None)
    return h


def _disks_of_volume(volume_mount):
    """挂载点(如 C:\\) → 物理盘号列表; 失败抛异常"""
    buf = ctypes.create_unicode_buffer(260)
    n = k32.GetVolumeNameForVolumeMountPointW(volume_mount, buf, 260)
    if not n:
        raise OSError('GetVolumeNameForVolumeMountPointW失败')
    guid = buf.value
    if guid.endswith('\\'):          # 带尾反斜杠打开的是根目录而不是卷
        guid = guid[:-1]
    h = _open_volume_zero_access(guid)
    if not h or h == INVALID_HANDLE_VALUE:
        raise OSError('打开卷失败(handle=%r)' % h)
    try:
        out = (ctypes.c_char * 512)()
        ret = wintypes.DWORD(0)
        ok = k32.DeviceIoControl(h, IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS,
                                 None, 0, out, 512, ctypes.byref(ret), None)
        if not ok:
            raise OSError('DeviceIoControl(GET_VOLUME_DISK_EXTENTS)失败')
        raw = bytes(out)[:ret.value]
        count = int.from_bytes(raw[0:4], 'little')
        disks = set()
        for i in range(min(count, 16)):        # 偏移8起每24字节一个DISK_EXTENT, 首DWORD是DiskNumber
            off = 8 + i * 24
            if off + 4 > len(raw):
                break
            disks.add(int.from_bytes(raw[off:off + 4], 'little'))
        return disks
    finally:
        k32.CloseHandle(h)


def _existing_ancestor(path):
    p = os.path.abspath(path)
    while not os.path.exists(p):
        np = os.path.dirname(p)
        if np == p:
            break
        p = np
    return p


def _backing_disks_of_path(path):
    """输出路径 → 物理盘号集合; 远程/RAM盘返回set(); 判不出返回None"""
    try:
        anc = _existing_ancestor(path)
        mount = ctypes.create_unicode_buffer(260)
        if not k32.GetVolumePathNameW(os.path.abspath(anc), mount, 260):
            return None
        dt = k32.GetDriveTypeW(mount.value)
        if dt in (DRIVE_REMOTE, DRIVE_RAMDISK):
            return set()
        return _disks_of_volume(mount.value)
    except Exception:
        return None


def ensure_output_off_source(source, outputs):
    """输出路径列表中有任何落在源盘上(或判不出所在盘) → 抛OutputOnSourceDrive"""
    src_disk = _physical_drive_of_source(source)
    if src_disk is None:
        return                      # 镜像文件等非物理盘源, 不受限
    for out in outputs:
        if not out:
            continue
        disks = _backing_disks_of_path(out)
        if disks is None:
            raise OutputOnSourceDrive(
                '无法判断输出 "%s" 所在的物理盘(出于安全考虑拒绝执行)——请换一个明确的位置' % out)
        if src_disk in disks:
            raise OutputOnSourceDrive(
                '输出 "%s" 在源盘 PhysicalDrive%d 上！写坏濒死盘就是这么毁数据的——请换到其他盘' % (out, src_disk))


if __name__ == '__main__':
    import sys
    src, out = sys.argv[1], sys.argv[2]
    try:
        ensure_output_off_source(src, [out])
        print('OK: 输出不在源盘')
    except OutputOnSourceDrive as e:
        print('拒绝:', e)
