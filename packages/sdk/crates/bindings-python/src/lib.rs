use mu_core::{ax, delta, image_point, matching};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use serde_json::Value;
fn parse(s: &str) -> PyResult<Value> {
    serde_json::from_str(s).map_err(|e| PyValueError::new_err(e.to_string()))
}
#[pyfunction]
fn is_mutation(message: &str) -> PyResult<bool> {
    let request: mu_protocol::Request =
        serde_json::from_str(message).map_err(|e| PyValueError::new_err(e.to_string()))?;
    Ok(request.is_mutation())
}
#[pyfunction]
fn match_nodes(nodes: &str, query: &str) -> PyResult<Vec<String>> {
    Ok(matching(&parse(nodes)?, &parse(query)?))
}
#[pyfunction]
fn diff_nodes(before: &str, after: &str) -> PyResult<String> {
    Ok(delta(&parse(before)?, &parse(after)?).to_string())
}
#[pyclass]
struct RuntimeCore {
    host: std::sync::Arc<mu_host::Host>,
}
#[pymethods]
impl RuntimeCore {
    #[new]
    fn new() -> Self {
        Self {
            host: mu_host::Host::new(),
        }
    }
    fn state(&self, request: &str) -> PyResult<String> {
        self.host
            .state(parse(request)?)
            .map(|v| v.to_string())
            .map_err(|e| PyValueError::new_err(serde_json::to_string(&e).unwrap()))
    }
    fn call<'py>(&self, py: Python<'py>, request: &str) -> PyResult<Bound<'py, PyAny>> {
        let host = self.host.clone();
        let q = parse(request)?;
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            host.call(q)
                .await
                .map(|v| v.to_string())
                .map_err(|e| PyValueError::new_err(serde_json::to_string(&e).unwrap()))
        })
    }
    fn acquire<'py>(&self, py: Python<'py>, domain: String) -> PyResult<Bound<'py, PyAny>> {
        let scheduler = self.host.runtime.core.lock().unwrap().scheduler.clone();
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            let guard = scheduler.acquire(&domain).await;
            Python::attach(|py| Py::new(py, Permit { guard: Some(guard) }))
        })
    }
    #[allow(clippy::too_many_arguments)]
    fn capture(
        &self,
        scope: &str,
        id: &str,
        data: &str,
        tree: bool,
        app: Option<&str>,
    ) -> PyResult<String> {
        let v=self.host.state(serde_json::json!({"op":"capture","scope":scope,"id":id,"data":parse(data)?,"tree":tree,"app":app})).map_err(|e|PyValueError::new_err(serde_json::to_string(&e).unwrap()))?;
        Ok(v.to_string())
    }
    #[allow(clippy::too_many_arguments)]
    fn node_wire(&self, scope: &str, id: &str, node: &str) -> PyResult<String> {
        let v = self
            .host
            .state(serde_json::json!({"op":"node_wire","scope":scope,"id":id,"node":node}))
            .map_err(|e| PyValueError::new_err(serde_json::to_string(&e).unwrap()))?;
        Ok(v.to_string())
    }
    #[allow(clippy::too_many_arguments)]
    fn resolve_index(&self, scope: &str, index: u64, in_turn: bool) -> PyResult<String> {
        let v=self.host.state(serde_json::json!({"op":"resolve_index","scope":scope,"index":index,"in_turn":in_turn})).map_err(|e|PyValueError::new_err(serde_json::to_string(&e).unwrap()))?;
        Ok(v.to_string())
    }
    #[allow(clippy::too_many_arguments)]
    fn point(
        &self,
        scope: &str,
        id: &str,
        token: &str,
        x: f64,
        y: f64,
        space: &str,
        max_age: u64,
    ) -> PyResult<String> {
        let v=self.host.state(serde_json::json!({"op":"point","scope":scope,"id":id,"token":token,"x":x,"y":y,"space":space,"max_age":max_age})).map_err(|e|PyValueError::new_err(serde_json::to_string(&e).unwrap()))?;
        Ok(v.to_string())
    }
    #[allow(clippy::too_many_arguments)]
    fn point_wire(&self, scope: &str, token: &str) -> PyResult<String> {
        let v = self
            .host
            .state(serde_json::json!({"op":"point_wire","scope":scope,"token":token}))
            .map_err(|e| PyValueError::new_err(serde_json::to_string(&e).unwrap()))?;
        Ok(v.to_string())
    }
    #[allow(clippy::too_many_arguments)]
    fn historical(&self, scope: &str, id: &str, image: bool) -> PyResult<bool> {
        let v = self
            .host
            .state(serde_json::json!({"op":"historical","scope":scope,"id":id,"image":image}))
            .map_err(|e| PyValueError::new_err(serde_json::to_string(&e).unwrap()))?;
        Ok(v.as_bool().unwrap_or(false))
    }
    #[allow(clippy::too_many_arguments)]
    fn render_capture(&self, scope: &str, id: &str, header: &str, full: bool) -> PyResult<String> {
        let v=self.host.state(serde_json::json!({"op":"render_capture","scope":scope,"id":id,"header":header,"full":full})).map_err(|e|PyValueError::new_err(serde_json::to_string(&e).unwrap()))?;
        Ok(v.to_string())
    }
    #[allow(clippy::too_many_arguments)]
    fn acknowledge(&self, scope: &str, id: &str, image: bool) -> PyResult<()> {
        let v = self
            .host
            .state(serde_json::json!({"op":"acknowledge","scope":scope,"id":id,"image":image}))
            .map_err(|e| PyValueError::new_err(serde_json::to_string(&e).unwrap()))?;
        let _ = v;
        Ok(())
    }
    #[allow(clippy::too_many_arguments)]
    fn failed_delivery(&self, scope: &str) -> PyResult<()> {
        let v = self
            .host
            .state(serde_json::json!({"op":"failed_delivery","scope":scope}))
            .map_err(|e| PyValueError::new_err(serde_json::to_string(&e).unwrap()))?;
        let _ = v;
        Ok(())
    }
    #[allow(clippy::too_many_arguments)]
    fn freeze(&self, scope: &str) -> PyResult<()> {
        let v = self
            .host
            .state(serde_json::json!({"op":"freeze","scope":scope}))
            .map_err(|e| PyValueError::new_err(serde_json::to_string(&e).unwrap()))?;
        let _ = v;
        Ok(())
    }
    #[allow(clippy::too_many_arguments)]
    fn reset(&self, scope: &str) -> PyResult<()> {
        let v = self
            .host
            .state(serde_json::json!({"op":"reset","scope":scope}))
            .map_err(|e| PyValueError::new_err(serde_json::to_string(&e).unwrap()))?;
        let _ = v;
        Ok(())
    }
    #[allow(clippy::too_many_arguments)]
    fn views(&self, scope: &str) -> PyResult<String> {
        let v = self
            .host
            .state(serde_json::json!({"op":"views","scope":scope}))
            .map_err(|e| PyValueError::new_err(serde_json::to_string(&e).unwrap()))?;
        Ok(v.to_string())
    }
    #[allow(clippy::too_many_arguments)]
    fn attach(&self, value: &str) -> PyResult<()> {
        let v = self
            .host
            .state(serde_json::json!({"op":"attach","attachment":parse(value)?}))
            .map_err(|e| PyValueError::new_err(serde_json::to_string(&e).unwrap()))?;
        let _ = v;
        Ok(())
    }
    #[allow(clippy::too_many_arguments)]
    fn check(&self, id: &str, write: bool) -> PyResult<()> {
        let v = self
            .host
            .state(serde_json::json!({"op":"check","id":id,"write":write}))
            .map_err(|e| PyValueError::new_err(serde_json::to_string(&e).unwrap()))?;
        let _ = v;
        Ok(())
    }
    #[allow(clippy::too_many_arguments)]
    fn detach(&self, id: &str) -> PyResult<()> {
        let v = self
            .host
            .state(serde_json::json!({"op":"detach","id":id}))
            .map_err(|e| PyValueError::new_err(serde_json::to_string(&e).unwrap()))?;
        let _ = v;
        Ok(())
    }
    #[allow(clippy::too_many_arguments)]
    fn begin(&self, session: &str, id: &str, request: &str) -> PyResult<String> {
        let v=self.host.state(serde_json::json!({"op":"begin","session":session,"id":id,"request":parse(request)?})).map_err(|e|PyValueError::new_err(serde_json::to_string(&e).unwrap()))?;
        Ok(v.to_string())
    }
    #[allow(clippy::too_many_arguments)]
    fn finish(&self, session: &str, id: &str, result: &str) -> PyResult<()> {
        let v=self.host.state(serde_json::json!({"op":"finish","session":session,"id":id,"result":parse(result)?})).map_err(|e|PyValueError::new_err(serde_json::to_string(&e).unwrap()))?;
        let _ = v;
        Ok(())
    }
    #[allow(clippy::too_many_arguments)]
    fn status(&self, session: &str, id: &str) -> PyResult<String> {
        let v = self
            .host
            .state(serde_json::json!({"op":"status","session":session,"id":id}))
            .map_err(|e| PyValueError::new_err(serde_json::to_string(&e).unwrap()))?;
        Ok(v.to_string())
    }
    fn image_bytes<'py>(
        &self,
        py: Python<'py>,
        scope: &str,
        id: &str,
    ) -> PyResult<Bound<'py, pyo3::types::PyBytes>> {
        let data = self
            .host
            .runtime
            .core
            .lock()
            .unwrap()
            .observations
            .image_bytes(scope, id)
            .map_err(PyValueError::new_err)?;
        Ok(pyo3::types::PyBytes::new(py, &data))
    }
}
#[pyclass]
struct Permit {
    guard: Option<tokio::sync::OwnedMutexGuard<()>>,
}
#[pymethods]
impl Permit {
    fn release(&mut self) {
        self.guard.take();
    }
}

#[pyclass]
struct ProcessClient {
    client: std::sync::Arc<mu_client::process::ProcessClient>,
}
#[pymethods]
impl ProcessClient {
    fn request<'py>(&self, py: Python<'py>, message: &str) -> PyResult<Bound<'py, PyAny>> {
        let request =
            serde_json::from_str(message).map_err(|e| PyValueError::new_err(format!("{e}")))?;
        let client = self.client.clone();
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            let reply = client
                .send(request)
                .await
                .map_err(|e| pyo3::exceptions::PyConnectionError::new_err(e.to_string()))?;
            serde_json::to_string(&reply).map_err(|e| PyValueError::new_err(e.to_string()))
        })
    }
    fn wait<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let client = self.client.clone();
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            client.wait().await;
            Ok(())
        })
    }
    fn close<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let client = self.client.clone();
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            client
                .shutdown()
                .await
                .map_err(|e| pyo3::exceptions::PyConnectionError::new_err(e.to_string()))
        })
    }
}
#[pyfunction]
fn connect_process(
    py: Python<'_>,
    program: String,
    args: Vec<String>,
    env: std::collections::BTreeMap<String, String>,
) -> PyResult<Bound<'_, PyAny>> {
    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let client = mu_client::process::ProcessClient::spawn(&program, &args, &env)
            .map_err(|e| pyo3::exceptions::PyConnectionError::new_err(e.to_string()))?;
        Python::attach(|py| Py::new(py, ProcessClient { client }))
    })
}
#[pyclass]
struct MacosClient {
    client: std::sync::Arc<mu_adapter_macos::MacosProvider>,
}
#[pymethods]
impl MacosClient {
    fn request<'py>(&self, py: Python<'py>, message: &str) -> PyResult<Bound<'py, PyAny>> {
        use mu_adapter::Provider;
        let request =
            serde_json::from_str(message).map_err(|e| PyValueError::new_err(format!("{e}")))?;
        let client = self.client.clone();
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            let result = client
                .request(request)
                .await
                .map_err(|e| PyValueError::new_err(e.to_string()))?;
            Ok(serde_json::to_string(&result).unwrap())
        })
    }
    fn close<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        use mu_adapter::Provider;
        let client = self.client.clone();
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            client
                .close()
                .await
                .map_err(|e| PyValueError::new_err(e.to_string()))
        })
    }
}
#[pyfunction]
#[pyo3(signature=(app_path=None,socket=None))]
fn connect_macos(
    py: Python<'_>,
    app_path: Option<String>,
    socket: Option<String>,
) -> PyResult<Bound<'_, PyAny>> {
    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let client = mu_adapter_macos::MacosProvider::connect_at(app_path, socket)
            .await
            .map_err(|e| PyValueError::new_err(e.to_string()))?;
        Python::attach(|py| Py::new(py, MacosClient { client }))
    })
}
#[pyfunction]
#[pyo3(signature=(operation,replace,archive,app_path=None,socket=None))]
fn macos_admin<'py>(
    py: Python<'py>,
    operation: String,
    replace: bool,
    archive: Option<String>,
    app_path: Option<String>,
    socket: Option<String>,
) -> PyResult<Bound<'py, PyAny>> {
    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let result = if operation == "setup" {
            tokio::task::spawn_blocking(move || {
                mu_adapter_macos::setup::install(replace, archive.as_deref())
            })
            .await
            .map_err(|e| PyValueError::new_err(e.to_string()))?
        } else {
            use mu_adapter::Provider;
            let client = mu_adapter_macos::MacosProvider::connect_at(app_path, socket)
                .await
                .map_err(|e| PyValueError::new_err(e.to_string()))?;
            let result = client.doctor().await;
            let _ = client.close().await;
            result
        };
        result
            .map(|v| v.to_string())
            .map_err(|e| PyValueError::new_err(e.to_string()))
    })
}
#[pyfunction]
fn browser_admin<'py>(
    py: Python<'py>,
    operation: String,
    engine: String,
) -> PyResult<Bound<'py, PyAny>> {
    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        mu_host::browser_admin::admin(&operation, &engine)
            .await
            .map(|v| v.to_string())
            .map_err(|e| PyValueError::new_err(serde_json::to_string(&e).unwrap()))
    })
}
#[pyfunction]
fn render_ax(request: &str) -> PyResult<String> {
    ax::render(&parse(request)?)
        .map(|v| v.to_string())
        .map_err(PyValueError::new_err)
}
#[pyfunction]
fn convert_point(
    x: f64,
    y: f64,
    space: &str,
    frame: (u32, u32),
    basis: (u32, u32),
) -> PyResult<(f64, f64)> {
    image_point(x, y, space, frame, basis).map_err(PyValueError::new_err)
}
#[pymodule]
fn _native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(browser_admin, m)?)?;
    m.add_class::<MacosClient>()?;
    m.add_function(wrap_pyfunction!(connect_macos, m)?)?;
    m.add_function(wrap_pyfunction!(macos_admin, m)?)?;
    m.add_class::<RuntimeCore>()?;
    m.add_class::<ProcessClient>()?;
    m.add_class::<Permit>()?;
    m.add_function(wrap_pyfunction!(connect_process, m)?)?;
    m.add_function(wrap_pyfunction!(is_mutation, m)?)?;
    m.add_function(wrap_pyfunction!(render_ax, m)?)?;
    m.add_function(wrap_pyfunction!(match_nodes, m)?)?;
    m.add_function(wrap_pyfunction!(diff_nodes, m)?)?;
    m.add_function(wrap_pyfunction!(convert_point, m)?)?;
    Ok(())
}
