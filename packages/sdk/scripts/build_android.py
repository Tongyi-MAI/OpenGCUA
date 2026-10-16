"""Build the owned development executor and isolated demo APK with installed SDK tools."""

from pathlib import Path
import os, subprocess

ROOT = Path(__file__).resolve().parents[1]
SDK = Path(os.environ.get("ANDROID_HOME", Path.home() / "Library/Android/sdk"))
TOOLS = SDK / "build-tools/35.0.0"
JAR = SDK / "platforms/android-35/android.jar"
OUT = ROOT / "adapters/android/out"
OUT.mkdir(parents=True, exist_ok=True)


def run(*args):
    subprocess.run([str(x) for x in args], check=True)


for name in ("agent", "demo"):
    classes = OUT / (name + "-classes")
    classes.mkdir(exist_ok=True)
    run(
        "javac",
        "-source",
        "11",
        "-target",
        "11",
        "-cp",
        JAR,
        "-d",
        classes,
        *sorted((ROOT / "adapters/android" / ("fixture" if name == "demo" else name) / "src").rglob("*.java")),
    )
    output = OUT / "agent.jar" if name == "agent" else OUT / "demo-dex"
    if name == "demo":
        output.mkdir(exist_ok=True)
    run(
        TOOLS / "d8",
        "--min-api",
        "30",
        "--lib",
        JAR,
        "--output",
        output,
        *sorted(classes.rglob("*.class")),
    )
manifest = OUT / "AndroidManifest.xml"
manifest.write_text("""<manifest xmlns:android="http://schemas.android.com/apk/res/android" package="dev.maiui.demo">
<uses-sdk android:minSdkVersion="30" android:targetSdkVersion="34"/>
<application android:label="mai-ui Demo" android:debuggable="true" android:theme="@android:style/Theme.Material.NoActionBar">
<activity android:name="dev.maiui.demo.MainActivity" android:exported="true"><intent-filter><action android:name="android.intent.action.MAIN"/><category android:name="android.intent.category.LAUNCHER"/></intent-filter></activity>
</application></manifest>""")
run(
    TOOLS / "aapt2",
    "link",
    "-I",
    JAR,
    "--manifest",
    manifest,
    "-o",
    OUT / "demo-unsigned.apk",
)
run("jar", "uf", OUT / "demo-unsigned.apk", "-C", OUT / "demo-dex", "classes.dex")
run(TOOLS / "zipalign", "-f", "4", OUT / "demo-unsigned.apk", OUT / "demo-aligned.apk")
key = OUT / "demo.keystore"
if not key.exists():
    run(
        "keytool",
        "-genkeypair",
        "-keystore",
        key,
        "-storepass",
        "mai-ui-demo",
        "-keypass",
        "mai-ui-demo",
        "-alias",
        "demo",
        "-dname",
        "CN=mai-ui Demo",
        "-keyalg",
        "RSA",
        "-validity",
        "3650",
    )
run(
    TOOLS / "apksigner",
    "sign",
    "--ks",
    key,
    "--ks-pass",
    "pass:mai-ui-demo",
    "--out",
    OUT / "demo.apk",
    OUT / "demo-aligned.apk",
)
run(TOOLS / "apksigner", "verify", OUT / "demo.apk")
print(OUT)
