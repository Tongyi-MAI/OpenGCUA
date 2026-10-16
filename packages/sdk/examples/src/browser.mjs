// Run after `mu setup browser --engine chromium` and building the Node binding.
import { Runtime } from "../packages/javascript/src/index.mjs";
const runtime = new Runtime();
try {
  await runtime.add_browser_factory("chromium", { engine: "chromium" });
  const mu = runtime.session({ output: (o) => console.log(o.text ?? o.type) });
  const web = await mu.browsers.create({
    name: "example",
    provider: "chromium",
  });
  const tab = await web.browser.new_tab();
  await tab.evaluate(
    "html=>{document.body.innerHTML=html;return null}",
    "<button onclick=\"this.textContent='Done'\">Continue</button>",
  );
  const obs = await tab.ui.ax.observe();
  await mu.emit(obs);
  await tab.ui.ax.click(obs.tree.nodes.find((n) => n.name === "Continue"));
  await mu.emit(await tab.ui.ax.observe());
} finally {
  await runtime.close();
}
