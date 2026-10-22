from __future__ import annotations

import os
import sys
from pathlib import Path

AGENT_ROOT = Path(__file__).resolve().parents[1]
if str(AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(AGENT_ROOT))

# 会话启动即绑定专用浏览器：测试里的 worker 不得启动真实 Chrome，需要的测试自行 delenv。
os.environ["MAC_AGENT_WEB"] = "off"
