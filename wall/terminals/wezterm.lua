-- WezTerm: Torment Nexus wall. Launch: wezterm --config-file wall/terminals/wezterm.lua start
local wezterm = require 'wezterm'
local config = wezterm.config_builder()
config.font = wezterm.font 'Menlo'
config.font_size = 4.0
config.line_height = 1.0
config.cell_width = 1.0
config.window_padding = { left = 0, right = 0, top = 0, bottom = 0 }
config.native_macos_fullscreen_mode = false -- use the strip beside the notch
config.window_decorations = 'RESIZE'
config.enable_tab_bar = false
config.default_cursor_style = 'SteadyBlock'
wezterm.on('gui-startup', function(cmd)
  local _, _, window = wezterm.mux.spawn_window(cmd or {})
  window:gui_window():toggle_fullscreen()
end)
return config
