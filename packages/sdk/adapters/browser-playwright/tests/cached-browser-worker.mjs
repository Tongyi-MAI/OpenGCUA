// Local diagnostic only. Release CI always uses the pinned Playwright binaries.
import { chromium } from "playwright";
const executablePath = process.env.MAI_UI_TEST_CHROMIUM_EXECUTABLE;
if (!executablePath)
  throw new Error(
    "Set the explicit existing Chromium executable for this diagnostic",
  );
const launch = chromium.launch.bind(chromium);
chromium.launch = (options) => launch({ ...options, executablePath });
await import("../dist/worker.js");
