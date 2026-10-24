//! Native core of ZecPy. Everything cryptographic happens in librustzcash; this crate adapts
//! it to Python.

mod cache;
mod error;
mod net;
mod network;
mod received;
mod send;
mod sync;
mod wallet;

use pyo3::prelude::*;

#[pymodule]
fn _native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<wallet::NativeWallet>()?;
    m.add_class::<wallet::Balance>()?;
    m.add_class::<received::ReceivedOutput>()?;
    error::register(m)?;
    Ok(())
}
