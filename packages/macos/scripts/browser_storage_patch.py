"""仅修补 App 内捆绑的 Playwright：登录态内部临时页必须在后台创建。

保留上游 cookies/localStorage/IndexedDB 序列化，不重写登录态引擎。
严格匹配已验证的 Chromium 服务代码；依赖升级不匹配时构建失败，禁止静默略过。
"""
from pathlib import Path

ORIGINAL = 'this._browser._session.send("Target.createTarget", { url: "about:blank", browserContextId: this._browserContextId })'
PATCHED = ('this._browser._session.send("Target.createTarget", { url: "about:blank", '
           'browserContextId: this._browserContextId, '
           '...(this.isCreatingStorageStatePage() ? { background: true } : {}) })')


def patch_storage_helpers(package: Path) -> None:
    """package 是 Playwright JS/driver 包；只在复制到构建目录的依赖上调用。"""
    bundle = package / "lib/coreBundle.js"
    text = bundle.read_text(encoding="utf-8")
    if ("isCreatingStorageStatePage()" not in text
            or "this._creatingStorageStatePage = !!forStorageState" not in text):
        raise ValueError(f"Playwright 登录态临时页标识已变化：{bundle}")
    if text.count(PATCHED) == 1 and text.count(ORIGINAL) == 0:
        return
    if text.count(ORIGINAL) != 1:
        raise ValueError(f"Playwright Chromium 建页代码不匹配，需重新验证补丁：{bundle}")
    bundle.write_text(text.replace(ORIGINAL, PATCHED), encoding="utf-8")
