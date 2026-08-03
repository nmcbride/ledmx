# Home Manager configuration for the ledmx daemon and GNOME hotkeys.
#
# This is an example to copy into your own configuration - importing it
# directly would couple your config to this repository's layout.
#
# Assumes `ledmx` is on PATH. With flakes, that usually means adding this
# repository's package to home.packages:
#
#   inputs.ledmx.url = "path:/home/nmcbride/git/framework-led-matrix";
#   home.packages = [ inputs.ledmx.packages.${pkgs.system}.default ];

{ config, lib, pkgs, ... }:

let
  ledmx = "${config.home.homeDirectory}/.nix-profile/bin/ledmx";
in
{
  # ── daemon ────────────────────────────────────────────────────────────
  #
  # A user service: panel access comes from a uaccess ACL granted to the
  # logged-in user, so running as that user is sufficient. Binding it to
  # graphical-session matters because the ACL only exists while a seat
  # session is active.
  systemd.user.services.ledmx = {
    Unit = {
      Description = "Framework 16 LED matrix display";
      After = [ "graphical-session.target" ];
      PartOf = [ "graphical-session.target" ];
    };
    Service = {
      Type = "simple";
      # --wait tolerates starting before the ACL has been applied at login.
      ExecStart = "${ledmx} daemon --scene monitor --wait 90";
      Restart = "on-failure";
      RestartSec = 5;
    };
    Install.WantedBy = [ "graphical-session.target" ];
  };

  # ── hotkeys ───────────────────────────────────────────────────────────
  #
  # GNOME custom keybindings. Each one runs a thin client that connects to
  # the daemon's socket, sends a line and exits - fast enough to feel
  # instant on a keypress.
  dconf.settings =
    let
      prefix = "org/gnome/settings-daemon/plugins/media-keys";
      binding = n: "${prefix}/custom-keybindings/ledmx-${n}";
    in
    {
      "${prefix}" = {
        custom-keybindings = map (n: "/${binding n}/") [
          "next" "prev" "monitor" "off"
        ];
      };

      "${binding "next"}" = {
        name = "LED matrix: next scene";
        command = "${ledmx} next";
        binding = "<Super><Alt>Right";
      };
      "${binding "prev"}" = {
        name = "LED matrix: previous scene";
        command = "${ledmx} prev";
        binding = "<Super><Alt>Left";
      };
      "${binding "monitor"}" = {
        name = "LED matrix: system monitor";
        command = "${ledmx} scene monitor";
        binding = "<Super><Alt>m";
      };
      "${binding "off"}" = {
        name = "LED matrix: blank";
        command = "${ledmx} scene off";
        binding = "<Super><Alt>o";
      };
    };
}
