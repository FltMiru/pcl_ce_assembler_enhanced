#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import hashlib
import json
import re
import shutil
import threading
import time
import zipfile
import configparser
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import List, Optional, Tuple
from urllib.parse import urlsplit

import requests
import tkinter as tk
from tkinter import messagebox, filedialog, ttk

UA = {"User-Agent": "PclCeFileAssembler"}
OFFICIAL = "https://launchermeta.mojang.com/mc/game/version_manifest_v2.json"
BMCL = "https://bmclapi2.bangbang93.com"
HOSTS = [
    "https://launchermeta.mojang.com",
    "https://piston-meta.mojang.com",
    "https://piston-data.mojang.com",
]
PREFIX = {
    "release": "R",
    "snapshot": "S",
    "old_beta": "B",
    "old_alpha": "O",
}
LABEL = {
    "release": "Release",
    "snapshot": "Snapshots",
    "old_beta": "Beta",
    "old_alpha": "old-alpha",
}
ILLEGAL = set(r'\/:*?"<>|')

# 默认限速（KB/s）
SPEED_LIMITS = [
    ("无限制", 0),
    ("500 KB/s", 500),
    ("1000 KB/s", 1000),
    ("2000 KB/s", 2000),
    ("4000 KB/s", 4000),
]

MODRINTH_API = "https://api.modrinth.com/v2"
CURSEFORGE_API = "https://api.curseforge.com/v1"

SOURCE_LABEL = {
    "builtin": "内置/离线",
    "modrinth": "Modrinth",
    "curseforge": "CurseForge",
}
FABRIC_META = "https://meta.fabricmc.net/v2"
QUILT_META = "https://meta.quiltmc.org/v3"
FORGE_PROMOS = "https://files.minecraftforge.net/net/minecraftforge/forge/promotions_slim.json"
NEOFORGE_MAVEN = "https://maven.neoforged.net/releases/net/neoforged/neoforge/maven-metadata.xml"
FORGE_MAVEN = "https://files.minecraftforge.net/maven/"
NEOFORGE_MAVEN_ROOT = "https://maven.neoforged.net/releases/"

def mirror(url: str, root: Optional[str]) -> str:
    if not root:
        return url
    for h in HOSTS:
        if url.startswith(h):
            return root + url[len(h):]
    return url


def limited_write(stream, chunk: bytes, speed_kb: int, progress_callback) -> None:
    if speed_kb > 0:
        delay = len(chunk) / (speed_kb * 1024)
        time.sleep(max(0, delay))
    stream.write(chunk)
    if progress_callback:
        progress_callback(len(chunk))


def download(
    url: str,
    dest: Path,
    root: Optional[str],
    sha1: Optional[str] = None,
    log_widget: Optional[tk.Text] = None,
    speed_kb: int = 0,
    progress_callback=None,
) -> bool:
    urls = [u for u in {mirror(url, root), url} if u]
    for _ in range(3):
        for u in urls:
            try:
                log(f"正在下载: {u}", log_widget)
                r = requests.get(u, headers=UA, timeout=15, stream=True)
                r.raise_for_status()

                total = int(r.headers.get("content-length", 0))
                received = 0

                dest.parent.mkdir(parents=True, exist_ok=True)
                with open(dest, "wb") as f:
                    for chunk in r.iter_content(chunk_size=65536):
                        if not chunk:
                            continue
                        limited_write(f, chunk, speed_kb, progress_callback)
                        received += len(chunk)
                        if progress_callback:
                            progress_callback(0, total, received)

                if sha1:
                    file_sha = hashlib.sha1(dest.read_bytes()).hexdigest().lower()
                    if file_sha != sha1.lower():
                        log("SHA1 校验失败，正在重试", log_widget)
                        dest.unlink(missing_ok=True)
                        continue

                log(f"已保存: {dest}", log_widget)
                return True

            except Exception as e:
                log(f"下载失败: {str(e)}", log_widget)
                dest.unlink(missing_ok=True)
    return False


def fetch_manifest(root: Optional[str], log_widget: Optional[tk.Text] = None):
    candidates = []
    if root:
        candidates.append(root.rstrip("/") + "/mc/game/version_manifest_v2.json")
    candidates.append(OFFICIAL)
    candidates.append(BMCL + "/mc/game/version_manifest_v2.json")
    seen = set()
    for url in candidates:
        if url in seen:
            continue
        seen.add(url)
        try:
            log(f"获取版本清单: {url}", log_widget)
            r = requests.get(url, headers=UA, timeout=15)
            r.raise_for_status()
            data = r.json()
            versions = data.get("versions", [])
            if versions:
                log(f"成功获取 {len(versions)} 个游戏版本", log_widget)
                return versions
        except Exception as e:
            log(f"获取清单失败: {e}", log_widget)
    raise Exception("无法获取版本清单，请检查网络连接")


def build_index(versions):
    idx = {}
    for typ, prefix in PREFIX.items():
        items = sorted(
            [v for v in versions if v.get("type") == typ],
            key=lambda x: x.get("releaseTime", ""),
        )
        for i, ver in enumerate(items):
            idx[f"{prefix}{i}"] = ver
    return idx


def resolve(raw: str, idx: dict):
    return idx.get(raw) or next(
        (v for v in idx.values() if v["id"] == raw),
        None,
    )


def valid_name(raw: str) -> Optional[str]:
    n = raw.strip()
    if not n:
        return None
    if n in (".", ".."):
        return None
    if set(n) & ILLEGAL:
        return None
    return n


def _log_direct(text: str, widget: tk.Text):
    try:
        widget.insert(tk.END, text + "\n")
        widget.see(tk.END)
        widget.update_idletasks()
    except Exception:
        pass


def log(text: str, widget: Optional[tk.Text] = None):
    if not widget:
        print(text)
        return
    try:
        if threading.current_thread() is threading.main_thread():
            _log_direct(text, widget)
        else:
            widget.after(0, lambda t=text: _log_direct(t, widget))
    except Exception:
        pass


def fmt_size(sz: int) -> str:
    if not sz:
        return "-"
    if sz < 1024:
        return f"{sz} B"
    if sz < 1024 * 1024:
        return f"{sz / 1024:.1f} KB"
    return f"{sz / (1024 * 1024):.1f} MB"


def url_filename(url: str) -> str:
    return Path(urlsplit(url).path).name or "mod.jar"


def safe_join(base: Path, rel: str) -> Path:
    base_r = base.resolve()
    dest = (base_r / rel).resolve()
    if not dest.is_relative_to(base_r):
        raise ValueError(f"非法路径，已拦截: {rel}")
    return dest

def detect_loader(data: dict) -> Optional[str]:
    text = (data.get("id") or "") + " " + (data.get("inheritsFrom") or "")
    for lib in data.get("libraries", []) or []:
        name = lib.get("name") if isinstance(lib, dict) else str(lib)
        text += " " + name
    low = text.lower()
    if "org.quiltmc:quilt-loader" in low or "quilt-loader" in low:
        return "quilt"
    if "net.fabricmc:fabric-loader" in low or "fabric-loader" in low:
        return "fabric"
    if "net.neoforged:neoforge" in low or "-neoforge-" in low:
        return "neoforge"
    if "net.minecraftforge:forge" in low or "-forge-" in low:
        return "forge"
    return None


def _latest_fabric(mc: str) -> Tuple[str, str]:
    r = requests.get(f"{FABRIC_META}/versions/loader/{mc}", headers=UA, timeout=20)
    items = r.json()
    item = next((x for x in items if x.get("stable")), items[0])
    return item["loader"]["version"], item["intermediary"]["version"]


def _latest_quilt(mc: str) -> Tuple[str, str]:
    r = requests.get(f"{QUILT_META}/versions/loader/{mc}", headers=UA, timeout=20)
    items = r.json()
    item = items[0]
    return item["loader"]["version"], item["intermediary"]["version"]


def _latest_forge(mc: str) -> Optional[str]:
    """Forge 官方 promotions 元数据：目标 MC 的推荐 / 最新构建号"""
    r = requests.get(FORGE_PROMOS, headers=UA, timeout=20)
    promos = r.json().get("promos", {})
    return promos.get(f"{mc}-recommended") or promos.get(f"{mc}-latest")


def _latest_neoforge(mc: str) -> Optional[str]:
    r = requests.get(NEOFORGE_MAVEN, headers=UA, timeout=20)
    versions = re.findall(r"<version>([^<]+)</version>", r.text)
    cands = sorted([v for v in versions if v.startswith(mc + "-")])
    return cands[-1] if cands else None


def _dedupe_args(args: list, target: list) -> list:
    seen = {repr(a) for a in target}
    for a in args:
        if repr(a) not in seen:
            target.append(a)
            seen.add(repr(a))
    return target


def _merge_profile(target: dict, prof: dict, orig: dict) -> dict:
    target.setdefault("libraries", [])
    existing = {lib.get("name") if isinstance(lib, dict) else lib for lib in target["libraries"]}
    for lib in prof.get("libraries", []) or []:
        nm = lib.get("name") if isinstance(lib, dict) else lib
        if nm not in existing:
            target["libraries"].append(lib)
            existing.add(nm)
    target["mainClass"] = prof.get("mainClass") or orig.get("mainClass")
    for key in ("game", "jvm"):
        prof_args = (prof.get("arguments") or {}).get(key) or []
        tgt = target.setdefault("arguments", {}).setdefault(key, [])
        _dedupe_args(prof_args, tgt)
    if prof.get("logging"):
        target["logging"] = prof["logging"]
    target.pop("inheritsFrom", None)
    return target


def _merge_arguments(target: dict, orig: dict) -> dict:
    for key in ("game", "jvm"):
        orig_args = (orig.get("arguments") or {}).get(key) or []
        tgt = target.setdefault("arguments", {}).setdefault(key, [])
        _dedupe_args(orig_args, tgt)
    return target


def apply_loader(target: dict, loader: str, mc: str, orig: dict) -> dict:
    if loader in ("fabric", "quilt"):
        lv, iv = _latest_fabric(mc) if loader == "fabric" else _latest_quilt(mc)
        base = FABRIC_META if loader == "fabric" else QUILT_META
        prof = requests.get(
            f"{base}/versions/loader/{mc}/{lv}/{iv}/profile/json",
            headers=UA, timeout=20,
        ).json()
        target = _merge_profile(target, prof, orig)
    elif loader == "forge":
        ver = _latest_forge(mc)
        if not ver:
            raise Exception(f"Forge 官方元数据中未找到 {mc} 的可用构建")
        target.setdefault("libraries", []).append({
            "name": f"net.minecraftforge:forge:{ver}", "url": FORGE_MAVEN,
        })
        target["mainClass"] = orig.get("mainClass") or "cpw.mods.modlauncher.Launcher"
        target = _merge_arguments(target, orig)
    elif loader == "neoforge":
        ver = _latest_neoforge(mc)
        if not ver:
            raise Exception(f"NeoForge 官方元数据中未找到 {mc} 的可用构建")
        target.setdefault("libraries", []).append({
            "name": f"net.neoforged:neoforge:{ver}", "url": NEOFORGE_MAVEN_ROOT,
        })
        target["mainClass"] = orig.get("mainClass") or "cpw.mods.modlauncher.Launcher"
        target = _merge_arguments(target, orig)
    target.pop("inheritsFrom", None)
    return target

class ModEntry:
    __slots__ = (
        "name", "source", "target_path", "url", "size", "sha1",
        "project_id", "file_id", "zip_member", "status", "row",
    )

    def __init__(
        self,
        source: str,
        target_path: str,
        name: str = None,
        url: str = None,
        size: int = 0,
        sha1: str = None,
        project_id: int = None,
        file_id: int = None,
        zip_member: str = None,
        status: str = "待处理",
    ):
        self.source = source
        self.target_path = target_path
        self.name = name or Path(target_path).name or "文件"
        self.url = url
        self.size = size
        self.sha1 = sha1
        self.project_id = project_id
        self.file_id = file_id
        self.zip_member = zip_member
        self.status = status
        self.row = None


def _parse_modrinth(zf: zipfile.ZipFile, names: List[str]):
    index = json.loads(zf.read("modrinth.index.json"))
    meta = {
        "name": index.get("name", "未命名整合包"),
        "mc_version": index.get("versionId") or (index.get("minecraft") or {}).get("version"),
        "loader": None,
    }
    loaders = index.get("modLoaders") or []
    if loaders:
        first = loaders[0]
        meta["loader"] = first.get("id") if isinstance(first, dict) else str(first)

    entries: List[ModEntry] = []
    seen = set()
    for f in index.get("files", []):
        path = (f.get("path") or "").strip("/")
        if not path:
            continue

        if path in names:
            entries.append(ModEntry(
                "builtin", path, zip_member=path, status="内置，待释放",
            ))
            seen.add(path)
            continue
        downloads = f.get("downloads") or []
        if downloads:
            hashes = f.get("hashes") or {}
            entries.append(ModEntry(
                "modrinth", path,
                name=Path(path).name,
                url=downloads[0],
                size=f.get("fileSize", 0),
                sha1=hashes.get("sha1"),
                status="待下载",
            ))
            seen.add(path)

    for n in names:
        if n.startswith("overrides/") and not n.endswith("/") and n not in seen:
            rel = n[len("overrides/"):]
            entries.append(ModEntry("builtin", rel, zip_member=n, status="内置，待释放"))
    return meta, entries


def _parse_curseforge(zf: zipfile.ZipFile, names: List[str]):
    manifest = json.loads(zf.read("manifest.json"))
    mc = manifest.get("minecraft", {})
    loaders = mc.get("modLoaders") or []
    if loaders and isinstance(loaders[0], dict):
        loader = loaders[0].get("id")
    elif loaders:
        loader = str(loaders[0])
    else:
        loader = None
    meta = {
        "name": manifest.get("name", "未命名整合包"),
        "mc_version": mc.get("version"),
        "loader": loader,
    }

    entries: List[ModEntry] = []
    for f in manifest.get("files", []):
        pid = f.get("projectID")
        fid = f.get("fileID")
        if pid and fid:
            entries.append(ModEntry(
                "curseforge", "mods/",
                name=f"CF #{pid} / 文件 #{fid}",
                project_id=pid, file_id=fid, status="待下载",
            ))

    override_dir = (manifest.get("overrides") or "overrides").strip("/")
    for n in names:
        if n.startswith(override_dir + "/") and not n.endswith("/"):
            rel = n[len(override_dir) + 1:]
            entries.append(ModEntry("builtin", rel, zip_member=n, status="内置，待释放"))
    return meta, entries


def parse_modpack(zip_path: Path):
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        if "modrinth.index.json" in names:
            return _parse_modrinth(zf, names)
        if "manifest.json" in names:
            return _parse_curseforge(zf, names)
    raise ValueError("无法识别的整合包格式（需要 manifest.json 或 modrinth.index.json）")


def parse_multimc(dir_path: Path):
    meta = {"name": dir_path.name, "mc_version": None, "loader": None}
    mmc_pack = dir_path / "mmc-pack.json"
    instance_cfg = dir_path / "instance.cfg"

    if mmc_pack.exists():
        j = json.loads(mmc_pack.read_text(encoding="utf-8"))
        if j.get("name"):
            meta["name"] = j["name"]
        for comp in j.get("components", []) or []:
            uid = (comp.get("uid") or "").lower()
            ver = comp.get("version")
            if uid == "net.minecraft" and ver:
                meta["mc_version"] = ver
            elif uid in ("net.minecraftforge", "net.neoforged",
                         "net.fabricmc.fabric-loader", "net.quiltmc.quilt-loader"):
                meta["loader"] = comp.get("cachedName") or comp.get("uid")
    elif instance_cfg.exists():
        cfg = configparser.ConfigParser()
        cfg.optionxform = str
        cfg.read(instance_cfg, encoding="utf-8")
        if cfg.has_section("Instance"):
            iv = cfg.get("Instance", "IntendedVersion", fallback=None)
            if iv:
                meta["mc_version"] = iv

    entries: List[ModEntry] = []
    EXCLUDE = {"mmc-pack.json", "instance.cfg"}
    for p in sorted(dir_path.rglob("*")):
        if not p.is_file():
            continue
        if p.name in EXCLUDE:
            continue
        rel = p.relative_to(dir_path).as_posix()
        if rel.startswith("versions/"):
            continue
        entries.append(ModEntry("builtin", rel, name=p.name,
                                zip_member=str(p), status="内置，待释放"))
    return meta, entries


def cf_download_url(project_id: int, file_id: int, api_key: str) -> str:
    if not api_key:
        raise ValueError("CurseForge 模组需要 API Key，请在整合包导入页填写")
    url = f"{CURSEFORGE_API}/mods/{project_id}/files/{file_id}/download-url"
    r = requests.get(url, headers={**UA, "x-api-key": api_key}, timeout=20)
    r.raise_for_status()
    return r.json()["data"]


def cf_mod_name(project_id: int, api_key: str) -> Optional[str]:
    if not api_key:
        return None
    try:
        url = f"{CURSEFORGE_API}/mods/{project_id}"
        r = requests.get(url, headers={**UA, "x-api-key": api_key}, timeout=20)
        if r.ok:
            return r.json()["data"]["name"]
    except Exception:
        pass
    return None

class ProgressWindow(tk.Toplevel):
    def __init__(self, parent):
        super().__init__(parent)
        self.title("下载进度")
        self.geometry("460x140")
        self.resizable(False, False)
        self.transient(parent)

        self.label_text = tk.StringVar(value="准备开始下载 ...")
        self.bytes_text = tk.StringVar(value="")

        frm = ttk.Frame(self, padding=12)
        frm.pack(fill=tk.BOTH, expand=True)

        ttk.Label(frm, textvariable=self.label_text, font=("微软雅黑", 10)).pack(
            anchor="w"
        )
        self.bar = ttk.Progressbar(frm, mode="determinate", length=420)
        self.bar.pack(pady=10, fill=tk.X)
        ttk.Label(frm, textvariable=self.bytes_text, foreground="#444444").pack(
            anchor="w"
        )

    def set_status(self, msg: str):
        self.label_text.set(msg)

    def set_bytes(self, current: int, total: int):
        if total > 0:
            pct = min(100, int(current / total * 100))
            self.bar["maximum"] = total
            self.bar["value"] = current
            self.bytes_text.set(f"{fmt_size(current)} / {fmt_size(total)} — {pct}%")
        else:
            self.bar["value"] = 0
            self.bytes_text.set(f"{fmt_size(current)}")

    def close(self):
        self.destroy()

class PclAssemblerGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("PCL CE 文件组装器 by FLT_MIRU@bilibili.com")
        self.root.geometry("840x760")
        self.root.minsize(780, 680)

        self.version_index: dict = {}
        self.version_list_data: list[Tuple[str, dict]] = []
        self.selected_version: Optional[dict] = None
        self.target_version: Optional[dict] = None
        self.modify_instance: Optional[Path] = None
        self.progress_win: Optional[ProgressWindow] = None
        self.mod_entries: List[ModEntry] = []
        self.modpack_meta: dict = {}

        self._apply_theme()
        self._build_ui()

    def _apply_theme(self):
        try:
            import sv_ttk
            sv_ttk.set_theme("light")
        except ImportError:
            pass

    def _build_ui(self):
        main = ttk.Frame(self.root, padding=12)
        main.pack(fill=tk.BOTH, expand=True)
        main.columnconfigure(0, weight=1)
        main.rowconfigure(0, weight=3)
        main.rowconfigure(1, weight=0)
        main.rowconfigure(2, weight=1)
        self.notebook = ttk.Notebook(main)
        self.notebook.grid(row=0, column=0, sticky="nsew")

        self._build_tab_vanilla()
        self._build_tab_modpack()
        self._build_tab_modify()

        self._build_global_options(main)

        log_box = ttk.LabelFrame(main, text="运行日志", padding=9)
        log_box.grid(row=2, column=0, sticky="nsew", pady=(8, 0))
        log_box.columnconfigure(0, weight=1)
        log_box.rowconfigure(0, weight=1)

        self.log_text = tk.Text(log_box, height=8, wrap="word", font=("Consolas", 10))
        log_scroll = ttk.Scrollbar(log_box, command=self.log_text.yview)
        self.log_text.config(yscrollcommand=log_scroll.set)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        log_scroll.grid(row=0, column=1, sticky="ns")

        self.dir_entry.bind("<FocusOut>", self.check_dir_input)
        self.mirror_entry.bind("<FocusOut>", self.check_mirror_input)
        self.game_dir_entry.bind("<FocusOut>", self.check_game_dir_input)
        self.name_entry.bind("<KeyRelease>", self.check_name_input)
        self.mod_name_entry.bind("<KeyRelease>", self.check_mod_name_input)

    def _build_tab_vanilla(self):
        tab = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(tab, text="原版组装")
        tab.columnconfigure(0, weight=1)
        tab.columnconfigure(1, weight=2)
        left = ttk.LabelFrame(tab, text="组装选项", padding=10)
        left.grid(row=0, column=0, sticky="nsew", padx=6)
        left.columnconfigure(1, weight=1)
        ttk.Label(left, text="下载限速：").grid(row=0, column=0, sticky="w", pady=5)
        self.speed_var = tk.StringVar(value="无限制")
        self.speed_combo = ttk.Combobox(
            left,
            textvariable=self.speed_var,
            state="readonly",
            values=[name for name, _ in SPEED_LIMITS],
        )
        self.speed_combo.grid(row=0, column=1, sticky="ew", padx=5)

        ttk.Label(left, text="自定义版本名：").grid(row=1, column=0, sticky="w", pady=5)
        self.name_var = tk.StringVar()
        self.name_entry = ttk.Entry(left, textvariable=self.name_var)
        self.name_entry.grid(row=1, column=1, sticky="ew", padx=5)
        self.name_err = ttk.Label(left, text="", foreground="#e03131", font=("微软雅黑", 9))
        self.name_err.grid(row=2, column=1, sticky="w", padx=5)
        btn_box = ttk.Frame(left)
        btn_box.grid(row=3, column=0, columnspan=2, pady=12)
        ttk.Button(btn_box, text="刷新版本列表", command=self.refresh_version_list).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_box, text="开始组装", command=self.start_build_thread).pack(side=tk.LEFT, padx=5)
        right = ttk.LabelFrame(tab, text="游戏版本列表（双击选中）", padding=10)
        right.grid(row=0, column=1, sticky="nsew", padx=6)
        right.rowconfigure(1, weight=1)
        right.columnconfigure(1, weight=1)
        ttk.Label(right, text="筛选版本").grid(row=0, column=0, sticky="w")
        self.search_var = tk.StringVar()
        self.search_entry = ttk.Entry(right, textvariable=self.search_var)
        self.search_entry.grid(row=0, column=1, sticky="ew", padx=5)
        self.search_entry.bind("<KeyRelease>", self.filter_version_list)
        list_container = ttk.Frame(right)
        list_container.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=8)
        list_container.columnconfigure(0, weight=1)
        list_container.rowconfigure(0, weight=1)
        self.version_list = tk.Listbox(
            list_container, height=16, selectmode=tk.SINGLE, font=("Consolas", 10),
        )
        scroll = ttk.Scrollbar(list_container, command=self.version_list.yview)
        self.version_list.config(yscrollcommand=scroll.set)
        self.version_list.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        self.version_list.bind("<Double-Button-1>", self.on_double_click_select)
    def _build_tab_modpack(self):
        tab = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(tab, text="整合包导入")
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(1, weight=1)
        pack_box = ttk.LabelFrame(tab, text="整合包与参数", padding=10)
        pack_box.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        pack_box.columnconfigure(1, weight=3)
        pack_box.columnconfigure(3, weight=2)
        ttk.Label(pack_box, text="整合包文件：").grid(row=0, column=0, sticky="w")
        file_row = ttk.Frame(pack_box)
        file_row.grid(row=0, column=1, sticky="ew", padx=5)
        file_row.columnconfigure(0, weight=1)
        self.modpack_var = tk.StringVar()
        self.modpack_entry = ttk.Entry(file_row, textvariable=self.modpack_var)
        self.modpack_entry.grid(row=0, column=0, sticky="ew")
        ttk.Button(file_row, text="浏览", command=self.choose_modpack).grid(row=0, column=1, padx=4)
        ttk.Button(file_row, text="浏览目录", command=self.choose_modpack_dir).grid(row=0, column=2, padx=4)
        ttk.Button(pack_box, text="解析整合包", command=self.parse_modpack).grid(
            row=0, column=2, sticky="w", padx=(10, 0)
        )
        ttk.Label(pack_box, text="目标版本名：").grid(row=1, column=0, sticky="w", pady=5)
        self.mod_name_var = tk.StringVar()
        self.mod_name_entry = ttk.Entry(pack_box, textvariable=self.mod_name_var)
        self.mod_name_entry.grid(row=1, column=1, sticky="ew", padx=5)
        self.mod_name_err = ttk.Label(pack_box, text="", foreground="#e03131", font=("微软雅黑", 9))
        self.mod_name_err.grid(row=2, column=1, sticky="w", padx=5)
        ttk.Label(pack_box, text="CurseForge API Key：").grid(row=1, column=2, sticky="w", padx=(10, 0))
        self.cf_key_var = tk.StringVar()
        self.cf_key_entry = ttk.Entry(pack_box, textvariable=self.cf_key_var, show="*")
        self.cf_key_entry.grid(row=1, column=3, columnspan=2, sticky="ew", padx=5)
        self.modpack_meta_var = tk.StringVar(value="尚未解析整合包")
        ttk.Label(pack_box, textvariable=self.modpack_meta_var, foreground="#444444").grid(
            row=3, column=0, columnspan=5, sticky="w", pady=(4, 0)
        )
        btn_box = ttk.Frame(pack_box)
        btn_box.grid(row=4, column=0, columnspan=5, sticky="w", pady=(8, 0))
        ttk.Button(btn_box, text="下载在线模组", command=self.start_modpack_download).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_box, text="开始导入", command=self.start_modpack_import).pack(side=tk.LEFT, padx=5)
        list_box = ttk.LabelFrame(
            tab, text="模组列表", padding=8,
        )
        list_box.grid(row=1, column=0, sticky="nsew")
        list_box.columnconfigure(0, weight=1)
        list_box.rowconfigure(0, weight=1)
        tree_frame = ttk.Frame(list_box)
        tree_frame.grid(row=0, column=0, sticky="nsew")
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)
        cols = ("名称", "来源", "大小", "状态")
        self.mod_tree = ttk.Treeview(
            tree_frame, columns=cols, show="headings", height=14,
        )
        self.mod_tree.heading("名称", text="模组 / 文件")
        self.mod_tree.heading("来源", text="来源")
        self.mod_tree.heading("大小", text="大小")
        self.mod_tree.heading("状态", text="状态")
        self.mod_tree.column("名称", width=280, anchor="w")
        self.mod_tree.column("来源", width=90, anchor="center")
        self.mod_tree.column("大小", width=90, anchor="e")
        self.mod_tree.column("状态", width=120, anchor="center")
        tree_scroll = ttk.Scrollbar(tree_frame, command=self.mod_tree.yview)
        self.mod_tree.config(yscrollcommand=tree_scroll.set)
        self.mod_tree.grid(row=0, column=0, sticky="nsew")
        tree_scroll.grid(row=0, column=1, sticky="ns")

    def _build_tab_modify(self):
        tab = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(tab, text="版本修改")
        tab.columnconfigure(0, weight=1)
        tab.columnconfigure(1, weight=2)
        tab.rowconfigure(1, weight=1)
        box = ttk.LabelFrame(tab, text="实例与版本", padding=10)
        box.grid(row=0, column=0, sticky="nsew", padx=6)
        box.columnconfigure(1, weight=1)
        ttk.Label(box, text="Minecraft 实例").grid(row=0, column=0, sticky="w", pady=5)
        inst_row = ttk.Frame(box)
        inst_row.grid(row=0, column=1, sticky="ew", padx=5)
        inst_row.columnconfigure(0, weight=1)
        self.instance_var = tk.StringVar()
        self.instance_combo = ttk.Combobox(inst_row, textvariable=self.instance_var, state="readonly")
        self.instance_combo.grid(row=0, column=0, sticky="ew")
        self.instance_combo.bind("<<ComboboxSelected>>", self.on_instance_selected)
        ttk.Button(inst_row, text="刷新", command=self.refresh_instances).grid(row=0, column=1, padx=(4, 0))
        ttk.Button(box, text="手动选择实例", command=self.choose_instance_dir).grid(
            row=0, column=2, sticky="w", padx=(10, 0)
        )
        self.instance_err = ttk.Label(box, text="", foreground="#e03131", font=("微软雅黑", 9))
        self.instance_err.grid(row=1, column=1, sticky="w", padx=5)

        ttk.Label(box, text="原始 Minecraft 版本").grid(row=2, column=0, sticky="w", pady=5)
        self.orig_mc_var = tk.StringVar(value="N/A")
        ttk.Label(box, textvariable=self.orig_mc_var).grid(row=2, column=1, sticky="w", padx=5)

        ttk.Label(box, text="目标 Minecraft 版本").grid(row=3, column=0, sticky="w", pady=5)
        ttk.Label(box, text="请在右侧选择").grid(row=3, column=1, sticky="w", padx=5)
        self.dl_client_var = tk.BooleanVar(value=False)
        self.keep_loader_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(box, text="同时下载 Client Jar", variable=self.dl_client_var).grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(4, 0)
        )
        ttk.Checkbutton(box, text="保留 Mod Loader 信息", variable=self.keep_loader_var).grid(
            row=5, column=0, columnspan=2, sticky="w", pady=(2, 0)
        )
        ttk.Label(
            box, text="要改装 Mod Loader，请使用 PCL CE 提供的实例修改功能。",
            foreground="#555555",
        ).grid(row=6, column=0, columnspan=2, sticky="w", pady=(6, 0))
        btn_box = ttk.Frame(box)
        btn_box.grid(row=7, column=0, columnspan=2, sticky="w", pady=(10, 0))
        self.modify_btn = ttk.Button(btn_box, text="开始修改", command=self.start_modify, state=tk.DISABLED)
        self.modify_btn.pack(side=tk.LEFT, padx=5)
        right = ttk.LabelFrame(tab, text="目标游戏版本列表（双击选中）", padding=10)
        right.grid(row=0, column=1, sticky="nsew", padx=6)
        right.rowconfigure(1, weight=1)
        right.columnconfigure(1, weight=1)
        ttk.Label(right, text="筛选版本：").grid(row=0, column=0, sticky="w")
        self.target_search_var = tk.StringVar()
        self.target_search_entry = ttk.Entry(right, textvariable=self.target_search_var)
        self.target_search_entry.grid(row=0, column=1, sticky="ew", padx=5)
        self.target_search_entry.bind("<KeyRelease>", self.filter_target_list)
        ttk.Button(right, text="刷新版本列表", command=self.refresh_version_list).grid(
            row=0, column=2, sticky="w", padx=(10, 0)
        )
        tgt_container = ttk.Frame(right)
        tgt_container.grid(row=1, column=0, columnspan=3, sticky="nsew", pady=8)
        tgt_container.columnconfigure(0, weight=1)
        tgt_container.rowconfigure(0, weight=1)
        self.target_version_list = tk.Listbox(
            tgt_container, height=16, selectmode=tk.SINGLE, font=("Consolas", 10),
        )
        tgt_scroll = ttk.Scrollbar(tgt_container, command=self.target_version_list.yview)
        self.target_version_list.config(yscrollcommand=tgt_scroll.set)
        self.target_version_list.grid(row=0, column=0, sticky="nsew")
        tgt_scroll.grid(row=0, column=1, sticky="ns")
        self.target_version_list.bind("<Double-Button-1>", self.on_target_double_click)

    def _build_global_options(self, main):
        box = ttk.LabelFrame(main, text="全局选项", padding=10)
        box.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        box.columnconfigure(1, weight=1)
        box.columnconfigure(4, weight=1)
        ttk.Label(box, text="下载源").grid(row=0, column=0, sticky="w")
        self.source_var = tk.StringVar(value="bmclapi")
        self.source_combo = ttk.Combobox(
            box,
            textvariable=self.source_var,
            state="readonly",
            width=14,
            values=["bmclapi", "official", "自定义镜像地址"],
        )
        self.source_combo.grid(row=0, column=1, sticky="ew", padx=5)
        self.source_combo.bind("<<ComboboxSelected>>", lambda _: self.check_mirror_input())
        ttk.Label(box, text="自定义镜像：").grid(row=0, column=2, sticky="w", padx=(14, 0))
        self.mirror_var = tk.StringVar()
        self.mirror_entry = ttk.Entry(box, textvariable=self.mirror_var)
        self.mirror_entry.grid(row=0, column=3, columnspan=2, sticky="ew", padx=5)
        ttk.Label(box, text="Versions 目录：").grid(row=1, column=0, sticky="w", pady=5)
        dir_row = ttk.Frame(box)
        dir_row.grid(row=1, column=1, sticky="ew", padx=5)
        dir_row.columnconfigure(0, weight=1)
        self.dir_var = tk.StringVar()
        self.dir_entry = ttk.Entry(dir_row, textvariable=self.dir_var)
        self.dir_entry.grid(row=0, column=0, sticky="ew")
        ttk.Button(dir_row, text="浏览", command=self.select_dir).grid(row=0, column=1, padx=(4, 0))
        self.dir_err = ttk.Label(box, text="", foreground="#e03131", font=("微软雅黑", 9))
        self.dir_err.grid(row=2, column=1, sticky="w", padx=5)

        ttk.Label(box, text="游戏目录：").grid(row=1, column=2, sticky="w", padx=(14, 0))
        gdir_row = ttk.Frame(box)
        gdir_row.grid(row=1, column=3, columnspan=2, sticky="ew", padx=5)
        gdir_row.columnconfigure(0, weight=1)
        self.game_dir_var = tk.StringVar()
        self.game_dir_entry = ttk.Entry(gdir_row, textvariable=self.game_dir_var)
        self.game_dir_entry.grid(row=0, column=0, sticky="ew")
        ttk.Button(gdir_row, text="浏览", command=self.select_game_dir).grid(row=0, column=1, padx=(4, 0))
        self.game_dir_err = ttk.Label(box, text="", foreground="#e03131", font=("微软雅黑", 9))
        self.game_dir_err.grid(row=2, column=3, sticky="w", padx=5)

        self.placeholder_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            box, text="仅创建空 Jar 占位文件", variable=self.placeholder_var,
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(2, 0))
        self.isolation_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            box, text="启用版本隔离", variable=self.isolation_var,
        ).grid(row=3, column=2, columnspan=2, sticky="w", padx=(14, 0))

    def check_dir_input(self, _=None) -> bool:
        raw = self.dir_var.get().strip()
        if not raw:
            self.dir_err.config(text="")
            return True
        try:
            Path(raw).expanduser().resolve()
            self.dir_err.config(text="")
            return True
        except Exception:
            self.dir_err.config(text="路径格式非法")
            return False

    def check_game_dir_input(self, _=None) -> bool:
        raw = self.game_dir_var.get().strip()
        if not raw:
            self.game_dir_err.config(text="")
            return True
        try:
            Path(raw).expanduser().resolve()
            self.game_dir_err.config(text="")
            return True
        except Exception:
            self.game_dir_err.config(text="路径格式非法")
            return False

    def check_name_input(self, _=None) -> bool:
        raw = self.name_var.get()
        if valid_name(raw) is None and raw.strip():
            self.name_err.config(text="含非法字符：\\ / : * ? \" < > |")
            return False
        self.name_err.config(text="")
        return True

    def check_mod_name_input(self, _=None) -> bool:
        raw = self.mod_name_var.get()
        if valid_name(raw) is None and raw.strip():
            self.mod_name_err.config(text="含非法字符：\\ / : * ? \" < > |")
            return False
        self.mod_name_err.config(text="")
        return True

    def check_mirror_input(self, _=None) -> bool:
        src = self.source_var.get().lower()
        url = self.mirror_var.get().strip()
        if src == "自定义镜像地址" and not url.startswith(("http://", "https://")):
            self.mirror_err.config(text="必须以 http / https 开头")
            return False
        self.mirror_err.config(text="")
        return True

    # ===================== 全局取值 =====================
    def _current_speed(self) -> int:
        for name, val in SPEED_LIMITS:
            if name == self.speed_var.get():
                return val
        return 0

    def _current_source(self) -> Optional[str]:
        src = self.source_var.get().lower()
        if src == "bmclapi":
            return BMCL
        if src == "official":
            return None
        return self.mirror_var.get().strip()

    def _all_version_lists(self):
        out = [self.version_list]
        if getattr(self, "target_version_list", None):
            out.append(self.target_version_list)
        return out

    def refresh_version_list(self):
        def task():
            try:
                versions = fetch_manifest(self._current_source(), self.log_text)
                idx = build_index(versions)
                self.version_index = idx
                data = []
                rows = []
                for _, typ in PREFIX.items():
                    group = sorted(
                        [
                            (k, v)
                            for k, v in idx.items()
                            if k.startswith(typ)
                        ],
                        key=lambda x: int(x[0][len(typ):]),
                    )
                    if not group:
                        continue
                    label = LABEL[next(k for k, v in PREFIX.items() if v == typ)]
                    rows.append(f"【{label}】")
                    for key, ver in group:
                        rows.append(f"{key:6} | {ver['id']}")
                        data.append((key, ver))
                self.version_list_data = data
                lists = self._all_version_lists()
                # 主线程单次回调一次性重建列表，避免跨线程调用 after 的不确定性
                self.root.after(
                    0, lambda l=lists, r=rows: self._populate_version_lists(l, r)
                )
                self.root.after(
                    0, lambda: log("版本列表刷新完成。双击选择版本。", self.log_text),
                )
            except Exception as e:
                self.root.after(0, lambda e=e: messagebox.showerror("刷新失败", str(e)))

        threading.Thread(target=task, daemon=True).start()

    def _populate_version_lists(self, lists, rows):
        for w in lists:
            w.delete(0, tk.END)
            for text in rows:
                w.insert(tk.END, text)

    def filter_version_list(self, _):
        keyword = self.search_var.get().lower()
        self.version_list.delete(0, tk.END)
        for key, ver in self.version_list_data:
            line = f"{key:6} | {ver['id']}"
            if keyword in line.lower():
                self.version_list.insert(tk.END, line)

    def filter_target_list(self, _):
        keyword = self.target_search_var.get().lower()
        self.target_version_list.delete(0, tk.END)
        for key, ver in self.version_list_data:
            line = f"{key:6} | {ver['id']}"
            if keyword in line.lower():
                self.target_version_list.insert(tk.END, line)

    def on_double_click_select(self, _):
        sel = self.version_list.curselection()
        if not sel:
            return
        text = self.version_list.get(sel[0])
        if text.startswith("【"):
            return
        key = text.split("|")[0].strip()
        self.selected_version = self.version_index.get(key)
        if self.selected_version:
            log(f"已选中: {self.selected_version['id']}", self.log_text)

    def on_target_double_click(self, _):
        sel = self.target_version_list.curselection()
        if not sel:
            return
        text = self.target_version_list.get(sel[0])
        if text.startswith("【"):
            return
        key = text.split("|")[0].strip()
        self.target_version = self.version_index.get(key)
        if self.target_version:
            log(f"目标版本: {self.target_version['id']}", self.log_text)
        self._update_modify_btn()

    def select_dir(self):
        path = filedialog.askdirectory(title="选择 PCL CE versions 文件夹")
        if path:
            self.dir_var.set(path)
            self.check_dir_input()
            self.refresh_instances()

    def select_game_dir(self):
        path = filedialog.askdirectory(title="选择游戏目录")
        if path:
            self.game_dir_var.set(path)
            self.check_game_dir_input()

    def refresh_instances(self):
        vdir = Path(self.dir_var.get().strip() or ".").expanduser().resolve()
        insts = []
        if vdir.exists():
            for child in sorted(vdir.iterdir()):
                if child.is_dir() and (child / f"{child.name}.json").exists():
                    insts.append(child.name)
        self.instance_combo["values"] = insts
        cur = self.instance_var.get()
        if cur not in insts:
            self.instance_var.set("")
            self.modify_instance = None
            self.orig_mc_var.set("[自动识别]")
        self._update_modify_btn()

    def on_instance_selected(self, _=None):
        self._load_instance(self.instance_var.get())

    def _load_instance(self, name: str):
        vdir = Path(self.dir_var.get().strip() or ".").expanduser().resolve()
        inst = vdir / name
        jpath = inst / f"{name}.json"
        if not jpath.exists():
            self.instance_err.config(text="找不到实例 JSON")
            self.modify_instance = None
            self._update_modify_btn()
            return
        try:
            data = json.loads(jpath.read_text(encoding="utf-8"))
            self.modify_instance = inst
            self.instance_err.config(text="")
            self.orig_mc_var.set(self._instance_mc(data) or "未知")
            log(f"已载入实例：{name}（原始版本 {self.orig_mc_var.get()}）", self.log_text)
        except Exception as e:
            self.instance_err.config(text=f"读取失败: {e}")
            self.modify_instance = None
        self._update_modify_btn()

    def _instance_mc(self, data: dict) -> Optional[str]:
        return data.get("inheritsFrom") or data.get("id")

    def choose_instance_dir(self):
        path = filedialog.askdirectory(title="选择实例文件夹")
        if not path:
            return
        p = Path(path)
        name = p.name
        if not (p / f"{name}.json").exists():
            messagebox.showwarning("无效实例", f"该文件夹缺少 {name}.json，无法作为实例。")
            return
        self.instance_var.set(name)
        self._load_instance(name)

    def _update_modify_btn(self):
        ready = bool(getattr(self, "modify_instance", None) and getattr(self, "target_version", None))
        self.modify_btn.config(state=tk.NORMAL if ready else tk.DISABLED)

    def _progress_callback(self, msg: str):
        def cb(recv: int, total: int = 0, current: int = 0):
            if self.progress_win and self.progress_win.winfo_exists():
                self.root.after(
                    0,
                    lambda: (
                        self.progress_win.set_status(msg),
                        self.progress_win.set_bytes(current or recv, total),
                    ),
                )
        return cb

    def start_build_thread(self):
        if not self.selected_version:
            messagebox.showwarning("未选择版本", "请先在列表中双击选中一个版本！")
            return
        if not all([self.check_dir_input(), self.check_name_input(), self.check_mirror_input()]):
            return
        threading.Thread(target=self.run_build, daemon=True).start()

    def run_build(self):
        try:
            root_url = self._current_source()
            speed_kb = self._current_speed()
            vdir = Path(self.dir_var.get()).expanduser().resolve()
            raw_name = self.name_var.get().strip()
            ver_name = valid_name(raw_name) if raw_name else self.selected_version["id"]
            placeholder = self.placeholder_var.get()
            vid = self.selected_version["id"]

            log(f"\n===== 开始组装：{vid} =====", self.log_text)
            log(f"目标目录：{vdir}", self.log_text)
            log(f"自定义名称：{ver_name}", self.log_text)
            if speed_kb > 0:
                log(f"下载限速：{speed_kb} KB/s", self.log_text)

            target = vdir / ver_name
            if target.exists() and not self._confirm_overwrite(target):
                log("用户取消。", self.log_text)
                return
            target.mkdir(parents=True, exist_ok=True)

            self._open_progress()
            self._assemble_base_version(
                self.selected_version, ver_name, target, root_url, speed_kb, placeholder,
            )
            self._close_progress()

            log("\n===== 组装完成！ =====", self.log_text)
            log(f"输出目录：{target}", self.log_text)
            log("打开 PCL CE，刷新版本列表即可。", self.log_text)
            self.root.after(
                0,
                lambda: messagebox.showinfo(
                    "完成",
                    "版本组装成功！\n在 PCL CE 刷新版本列表，然后选择'修补核心'补全文件。\n要补装Mod Loader，请核心修复完成后使用修改功能。",
                ),
            )
        except Exception as exc:
            self._close_progress()
            log(f"\n任务失败：{str(exc)}", self.log_text)
            self.root.after(0, lambda: messagebox.showerror("组装失败", str(exc)))

    def _assemble_base_version(self, ver, ver_name, target, root_url, speed_kb, placeholder):
        """组装一个基础游戏版本：version.json + client.jar（或空占位）+ natives"""
        self._progress_status("正在下载版本配置 JSON")
        jpath = target / f"{ver_name}.json"
        if not download(
            ver["url"], jpath, root_url,
            log_widget=self.log_text, speed_kb=speed_kb,
            progress_callback=self._progress_callback("下载版本配置"),
        ):
            raise Exception("版本 JSON 下载失败")

        data = json.loads(jpath.read_text(encoding="utf-8"))
        data["id"] = ver_name
        jpath.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"已更新版本 ID → {ver_name}", self.log_text)

        jar_path = target / f"{ver_name}.jar"
        if placeholder:
            self._progress_status("创建空占位 Jar")
            jar_path.touch()
            log("创建空占位 Jar", self.log_text)
        else:
            self._progress_status("正在下载 client.jar")
            client = data.get("downloads", {}).get("client")
            if not client:
                raise Exception("版本信息中缺少 client 下载项")
            if not download(
                client["url"], jar_path, root_url, client.get("sha1"),
                log_widget=self.log_text, speed_kb=speed_kb,
                progress_callback=self._progress_callback("下载 client.jar"),
            ):
                raise Exception("client.jar 下载失败")

        self._progress_status("创建 natives 目录")
        (target / f"{ver_name}-natives").mkdir(exist_ok=True)
        return data

    def _confirm_overwrite(self, target: Path) -> bool:
        from threading import Event
        result: dict = {}
        done = Event()

        def ask():
            result["ok"] = bool(messagebox.askyesno(
                "文件夹已存在", f"目标目录已存在：\n{target}\n是否覆盖？",
            ))
            done.set()

        self.root.after(0, ask)
        done.wait()
        return result.get("ok", False)

    def _open_progress(self):
        self.root.after(0, lambda: setattr(self, "progress_win", ProgressWindow(self.root)))

    def _close_progress(self):
        win = self.progress_win
        if win and win.winfo_exists():
            self.root.after(0, win.close)

    def _progress_status(self, msg: str):
        self.root.after(0, lambda: self.progress_win.set_status(msg) if self.progress_win else None)

    def start_modify(self):
        if not self.modify_instance:
            messagebox.showwarning("未选择实例", "请先选择一个 Minecraft 实例！")
            return
        if not self.target_version:
            messagebox.showwarning("未选择目标版本", "请在版本列表中双击选中目标版本！")
            return
        if not all([self.check_dir_input(), self.check_mirror_input()]):
            return
        threading.Thread(target=self.run_modify, daemon=True).start()

    def run_modify(self):
        try:
            inst = self.modify_instance
            target = self.target_version
            name = inst.name
            jpath = inst / f"{name}.json"
            root_url = self._current_source()
            speed_kb = self._current_speed()
            keep_loader = self.keep_loader_var.get()
            dl_client = self.dl_client_var.get()
            log(f"\n===== 开始修改实例：{name} =====", self.log_text)
            log(f"目标版本：{target['id']}", self.log_text)
            orig = json.loads(jpath.read_text(encoding="utf-8"))
            orig_mc = self._instance_mc(orig)
            loader = detect_loader(orig)
            log(f"原始版本：{orig_mc}    检测到 Mod Loader：{loader or '无'}", self.log_text)
            self._open_progress()
            tmp = inst / f".{name}.json.tmp"
            if not download(
                target["url"], tmp, root_url,
                log_widget=self.log_text, speed_kb=speed_kb,
                progress_callback=self._progress_callback("下载版本配置"),
            ):
                raise Exception("目标版本 JSON 下载失败")
            data = json.loads(tmp.read_text(encoding="utf-8"))
            tmp.unlink(missing_ok=True)
            data["id"] = name

            if keep_loader and loader:
                log(f"保留 Mod Loader：{loader}，正在按官方元数据解析适配...", self.log_text)
                data = apply_loader(data, loader, target["id"], orig)
                log("已按官方文档合并 Mod Loader 配置。", self.log_text)
            else:
                data.pop("inheritsFrom", None)

            jpath.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            log(f"已替换实例 JSON：{jpath}", self.log_text)

            if dl_client:
                self._progress_status("正在下载 client.jar")
                client = data.get("downloads", {}).get("client")
                if client:
                    if not download(
                        client["url"], inst / f"{name}.jar", root_url, client.get("sha1"),
                        log_widget=self.log_text, speed_kb=speed_kb,
                        progress_callback=self._progress_callback("下载 client.jar"),
                    ):
                        raise Exception("client.jar 下载失败")
                else:
                    log("版本信息中缺少 client 下载项，跳过。", self.log_text)

            self._close_progress()
            log("\n===== 实例修改完成！ =====", self.log_text)
            log("打开 PCL CE 刷新版本列表即可。", self.log_text)
            self.root.after(
                0,
                lambda: messagebox.showinfo(
                    "完成",
                    f"实例「{name}」已修改为目标版本 {target['id']}。\n请刷新 PCL CE 版本列表。",
                ),
            )
        except Exception as exc:
            self._close_progress()
            log(f"\n修改失败：{str(exc)}", self.log_text)
            self.root.after(0, lambda: messagebox.showerror("修改失败", str(exc)))

    def choose_modpack(self):
        path = filedialog.askopenfilename(
            title="选择整合包文件",
            filetypes=[("整合包", "*.zip *.mrpack"), ("所有文件", "*.*")],
        )
        if path:
            self.modpack_var.set(path)

    def choose_modpack_dir(self):
        path = filedialog.askdirectory(title="选择 MultiMC / Prism 实例目录")
        if path:
            self.modpack_var.set(path)

    def parse_modpack(self):
        path = self.modpack_var.get().strip()
        if not path or not Path(path).exists():
            messagebox.showwarning("未选择文件", "请先选择整合包文件或实例目录")
            return
        threading.Thread(target=self._parse_modpack_worker, args=(path,), daemon=True).start()

    def _parse_modpack_worker(self, path):
        try:
            p = Path(path)
            log(f"正在解析整合包: {path}", self.log_text)
            if p.is_dir():
                meta, entries = parse_multimc(p)
            else:
                meta, entries = parse_modpack(p)
            self.modpack_meta = meta
            self.mod_entries = entries
            self.root.after(0, lambda: self._populate_mod_tree(meta, entries))
            # 后台尝试拉取 CurseForge 模组名称，美化列表
            threading.Thread(target=self._resolve_cf_names, args=(entries,), daemon=True).start()
        except Exception as e:
            log(f"解析失败: {e}", self.log_text)
            self.root.after(0, lambda e=e: messagebox.showerror("解析失败", str(e)))

    def _populate_mod_tree(self, meta, entries):
        self.mod_tree.delete(*self.mod_tree.get_children())
        for e in entries:
            row = self.mod_tree.insert(
                "", tk.END,
                values=(
                    e.name,
                    SOURCE_LABEL.get(e.source, e.source),
                    fmt_size(e.size),
                    e.status,
                ),
            )
            e.row = row
        builtin = sum(1 for e in entries if e.source == "builtin")
        online = len(entries) - builtin
        self.modpack_meta_var.set(
            f"整合包：{meta.get('name', '')}    目标游戏版本：{meta.get('mc_version', '未知')}"
            f"    模组加载器：{meta.get('loader', '未知')}"
        )
        log(
            f"解析完成：共 {len(entries)} 个文件"
            f"（内置/离线 {builtin}，联网下载 {online}）。",
            self.log_text,
        )
        if online:
            log("联网模组将后台调用 Modrinth / CurseForge API 下载，状态见列表。", self.log_text)

    def _update_mod_row(self, e: ModEntry):
        if e.row is None:
            return
        self.root.after(0, lambda: self._set_mod_row(e))

    def _set_mod_row(self, e: ModEntry):
        if not self.mod_tree.winfo_exists():
            return
        try:
            self.mod_tree.set(e.row, "名称", e.name)
            self.mod_tree.set(e.row, "来源", SOURCE_LABEL.get(e.source, e.source))
            self.mod_tree.set(e.row, "大小", fmt_size(e.size))
            self.mod_tree.set(e.row, "状态", e.status)
        except tk.TclError:
            pass

    def _resolve_cf_names(self, entries, api_key=None):
        key = api_key or self.cf_key_var.get().strip()
        if not key:
            return
        for e in entries:
            if e.source != "curseforge":
                continue
            name = cf_mod_name(e.project_id, key)
            if name:
                e.name = name
                self._update_mod_row(e)

    def start_modpack_download(self):
        if not self.mod_entries:
            messagebox.showwarning("未解析", "请先解析整合包")
            return
        threading.Thread(target=self._run_online_download, daemon=True).start()

    def _run_online_download(self):
        try:
            self._open_progress()
            self._download_online_mods(
                self.mod_entries, self._mod_dest_base(), speed_kb=self._current_speed(),
            )
            self._close_progress()
            failed = [e for e in self.mod_entries if e.status.startswith("失败")]
            if failed:
                log(f"\n下载完成，但有 {len(failed)} 个失败。", self.log_text)
                self.root.after(
                    0,
                    lambda: messagebox.showwarning("部分失败", f"{len(failed)} 个模组下载失败，请查看列表状态。"),
                )
            else:
                log("\n全部在线模组下载完成。", self.log_text)
        except Exception as exc:
            self._close_progress()
            log(f"\n下载任务失败：{str(exc)}", self.log_text)
            self.root.after(0, lambda: messagebox.showerror("下载失败", str(exc)))

    def start_modpack_import(self):
        if not self.mod_entries:
            messagebox.showwarning("未解析", "请先解析整合包")
            return
        if not all([self.check_dir_input(), self.check_mod_name_input(), self.check_mirror_input()]):
            return
        threading.Thread(target=self.run_modpack_import, daemon=True).start()

    def run_modpack_import(self):
        try:
            root_url = self._current_source()
            speed_kb = self._current_speed()
            placeholder = self.placeholder_var.get()
            isolation = self.isolation_var.get()
            vdir = Path(self.dir_var.get()).expanduser().resolve()
            src_path = Path(self.modpack_var.get())
            meta = self.modpack_meta

            mc_id = meta.get("mc_version")
            if not mc_id:
                raise Exception("整合包未声明目标游戏版本，无法组装基础版本")

            ver_name = self._modpack_target_name(meta)
            version_folder = vdir / ver_name

            log(f"\n===== 开始导入整合包：{meta.get('name')} =====", self.log_text)
            log(f"目标游戏版本：{mc_id}    模组加载器：{meta.get('loader')}", self.log_text)
            log(f"版本目录：{version_folder}", self.log_text)

            if version_folder.exists() and not self._confirm_overwrite(version_folder):
                log("用户取消。", self.log_text)
                return
            version_folder.mkdir(parents=True, exist_ok=True)
            self._open_progress()
            if not self.version_index:
                versions = fetch_manifest(root_url, self.log_text)
                self.version_index = build_index(versions)
            ver = resolve(mc_id, self.version_index)
            if not ver:
                raise Exception(f"在版本清单中找不到游戏版本 {mc_id}")
            self._assemble_base_version(ver, ver_name, version_folder, root_url, speed_kb, placeholder)
            if isolation:
                dest_base = version_folder
                log("版本隔离已开启：模组/配置写入独立版本目录。", self.log_text)
            else:
                gdir = self.game_dir_var.get().strip()
                dest_base = Path(gdir).expanduser().resolve() if gdir else vdir.parent
                log(f"版本隔离已关闭：模组/配置写入共享游戏目录 {dest_base}", self.log_text)

            self._extract_builtin(src_path, dest_base)
            self._download_online_mods(self.mod_entries, dest_base, speed_kb=speed_kb)

            self._close_progress()
            log("\n===== 整合包导入完成！ =====", self.log_text)
            log(f"输出目录：{version_folder}", self.log_text)
            log("打开 PCL CE，刷新版本列表即可。", self.log_text)
            self.root.after(
                0,
                lambda: messagebox.showinfo(
                    "完成",
                    "整合包导入成功！\n基础版本已组装 模组已装配完毕。\nMod Loader 请使用 PCL 的修改功能补装。",
                ),
            )
        except Exception as exc:
            self._close_progress()
            log(f"\n导入失败：{str(exc)}", self.log_text)
            self.root.after(0, lambda: messagebox.showerror("导入失败", str(exc)))

    def _modpack_target_name(self, meta) -> str:
        raw = self.mod_name_var.get().strip()
        if raw:
            return valid_name(raw)
        base = valid_name(meta.get("name") or "整合包") or "整合包"
        ver = meta.get("mc_version") or ""
        return f"{base}-{ver}" if ver else base

    def _extract_builtin(self, src_path: Path, dest_base: Path):
        if src_path.is_dir():
            for e in self.mod_entries:
                if e.source != "builtin" or not e.zip_member:
                    continue
                s = Path(e.zip_member)
                if not s.is_file():
                    continue
                out = safe_join(dest_base, e.target_path)
                out.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(s, out)
                e.status = "已释放"
                self._update_mod_row(e)
        else:
            with zipfile.ZipFile(src_path) as zf:
                for e in self.mod_entries:
                    if e.source != "builtin" or not e.zip_member:
                        continue
                    try:
                        info = zf.getinfo(e.zip_member)
                    except KeyError:
                        continue
                    if info.is_dir():
                        continue
                    out = safe_join(dest_base, e.target_path)
                    out.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(e.zip_member) as src, open(out, "wb") as dst:
                        dst.write(src.read())
                    e.status = "已释放"
                    self._update_mod_row(e)
        log("内置/离线模组已释放。", self.log_text)

    def _download_online_mods(self, entries, dest_base: Path, speed_kb=0, root_url=None):
        online = [e for e in entries if e.source in ("modrinth", "curseforge")]
        if not online:
            log("没有需要联网下载的模组。", self.log_text)
            return
        cf_key = self.cf_key_var.get().strip()
        total = len(online)
        done = 0
        log(f"开始后台下载 {total} 个在线模组...", self.log_text)
        self._progress_status(f"正在后台下载在线模组 0/{total}")

        with ThreadPoolExecutor(max_workers=4) as ex:
            futures = {
                ex.submit(self._download_one_mod, e, dest_base, speed_kb, cf_key): e
                for e in online
            }
            for fut in as_completed(futures):
                done += 1
                e = futures[fut]
                try:
                    fut.result()
                except Exception as err:
                    e.status = f"失败: {err}"
                    self._update_mod_row(e)
                self._progress_status(f"在线模组下载 {done}/{total}")

        succ = sum(1 for e in online if e.status == "完成")
        log(f"在线模组下载结束：成功 {succ}/{total}。", self.log_text)

    def _download_one_mod(self, e: ModEntry, dest_base: Path, speed_kb: int, cf_key: str) -> bool:
        e.status = "下载中"
        self._update_mod_row(e)
        try:
            if e.source == "curseforge":
                url = cf_download_url(e.project_id, e.file_id, cf_key)
            else:
                url = e.url
            if not url:
                e.status = "无下载地址"
                self._update_mod_row(e)
                return False

            # 目标路径：curseforge 的 target_path 是目录，文件名取自下载地址
            if e.source == "curseforge" or e.target_path.endswith("/") or e.target_path == "":
                dest = safe_join(dest_base, e.target_path) / url_filename(url)
            else:
                dest = safe_join(dest_base, e.target_path)

            ok = download(
                url, dest, None, e.sha1,
                log_widget=self.log_text, speed_kb=speed_kb,
                progress_callback=self._progress_callback(f"下载模组 {e.name}"),
            )
            e.status = "完成" if ok else "失败"
        except Exception as err:
            e.status = f"失败: {err}"
        self._update_mod_row(e)
        return e.status == "完成"

    def _mod_dest_base(self) -> Path:
        vdir = Path(self.dir_var.get()).expanduser().resolve()
        if self.isolation_var.get():
            base = vdir / self._modpack_target_name(self.modpack_meta)
        else:
            gdir = self.game_dir_var.get().strip()
            base = Path(gdir).expanduser().resolve() if gdir else vdir.parent
        base.mkdir(parents=True, exist_ok=True)
        return base


if __name__ == "__main__":
    app_root = tk.Tk()
    app = PclAssemblerGUI(app_root)
    app_root.mainloop()
