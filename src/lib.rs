//! Native core of ZecPy. Everything cryptographic happens in librustzcash; this crate adapts
//! it to Python.

mod cache;
mod error;
mod net;
mod network;
mod wallet;

use pyo3::prelude::*;

#[pymodule]
fn _native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<wallet::NativeWallet>()?;
    m.add_class::<wallet::Balance>()?;
    error::register(m)?;
    Ok(())
}
