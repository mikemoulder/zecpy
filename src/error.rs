//! Python exception types. Messages never include key material: library errors are mapped
//! by their `Display`, which in librustzcash does not print seeds or keys.

use std::fmt::Display;

use pyo3::create_exception;
use pyo3::exceptions::PyException;
use pyo3::prelude::*;

create_exception!(_native, ZecPyError, PyException, "Base class for ZecPy errors.");
create_exception!(_native, NetworkError, ZecPyError, "The light server could not be reached or returned an error.");
create_exception!(_native, WalletError, ZecPyError, "The wallet database rejected an operation.");
create_exception!(_native, PrivacyError, ZecPyError, "The operation would leak information and was refused.");

pub fn network<E: Display>(context: &str) -> impl FnOnce(E) -> PyErr + '_ {
    move |e| NetworkError::new_err(format!("{context}: {e}"))
}

pub fn wallet<E: Display>(context: &str) -> impl FnOnce(E) -> PyErr + '_ {
    move |e| WalletError::new_err(format!("{context}: {e}"))
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    let py = m.py();
    m.add("ZecPyError", py.get_type::<ZecPyError>())?;
    m.add("NetworkError", py.get_type::<NetworkError>())?;
    m.add("WalletError", py.get_type::<WalletError>())?;
    m.add("PrivacyError", py.get_type::<PrivacyError>())?;
    Ok(())
}
