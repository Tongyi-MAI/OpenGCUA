// Force the worker to prove it does not use Playwright's focus-stealing newPage.
import { chromium } from "playwright";

const launch = chromium.launch.bind(chromium);
chromium.launch = async (options) => {
  const browser = await launch(options);
  const newContext = browser.newContext.bind(browser);
  browser.newContext = async (contextOptions) => {
    const context = await newContext(contextOptions);
    context.newPage = async () => {
      throw new Error("foreground newPage is forbidden in background mode");
    };
    return context;
  };
  return browser;
};

await import("../dist/worker.js");
