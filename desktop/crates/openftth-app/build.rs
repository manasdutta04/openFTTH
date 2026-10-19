//! Windows: embed the app icon and version details in openftth.exe, so Explorer, the Start
//! menu, shortcuts and the taskbar show the openFTTH mark.

fn main() {
    println!("cargo:rerun-if-changed=assets/openftth.ico");
    #[cfg(windows)]
    {
        let mut res = winresource::WindowsResource::new();
        res.set_icon("assets/openftth.ico")
            .set("ProductName", "openFTTH")
            .set("FileDescription", "openFTTH: fibre access design with a crew")
            .set("LegalCopyright", "MIT licence");
        if let Err(e) = res.compile() {
            println!("cargo:warning=app icon not embedded: {e}");
        }
    }
}
