//! Dictation: the mic button opens Windows voice typing (the Win + H panel) on the composer.
//!
//! Windows voice typing is part of Windows 10 and 11, free, works in every language Windows
//! supports, and types straight into the focused text field through the input method, so the
//! words land in the composer as you speak. Pressing the button again closes the panel.

/// Toggle the Windows voice typing panel. The composer must already have keyboard focus.
#[cfg(windows)]
pub fn toggle() -> bool {
    #[link(name = "user32")]
    extern "system" {
        fn keybd_event(vk: u8, scan: u8, flags: u32, extra: usize);
    }
    const VK_LWIN: u8 = 0x5B;
    const VK_H: u8 = 0x48;
    const KEYUP: u32 = 0x0002;
    // SAFETY: keybd_event only queues synthetic key events for the foreground window.
    unsafe {
        keybd_event(VK_LWIN, 0, 0, 0);
        keybd_event(VK_H, 0, 0, 0);
        keybd_event(VK_H, 0, KEYUP, 0);
        keybd_event(VK_LWIN, 0, KEYUP, 0);
    }
    true
}

#[cfg(not(windows))]
pub fn toggle() -> bool {
    false
}
