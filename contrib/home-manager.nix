# Home Manager configuration for the ledmx daemon and GNOME hotkeys.
#
# An example to adapt, not to import - importing it directly would couple your
# configuration to this repository's layout.
#
# Add the flake as an input and pass the package in:
#
#   inputs.ledmx.url = "github:nmcbride/framework-led-matrix";  # or path:...
#   home.packages = [ inputs.ledmx.packages.${pkgs.system}.default ];
#
# Note what `ledmxPackage` buys over a hand-written path. Interpolating the
# derivation makes the unit reference an exact store path that is a genuine
# dependency of your configuration: it cannot be garbage-collected while the
# generation exists, and it updates on rebuild. A literal /nix/store path
# written into a unit file has neither property - it is not a GC root, and it
# pins the service to one build forever.

{ config, lib, pkgs, ... }:

let
  # Replace with your flake input, e.g.
  #   inputs.ledmx.packages.${pkgs.stdenv.hostPlatform.system}.default
  ledmxPackage = pkgs.ledmx or (throw "set ledmxPackage to the ledmx derivation");
  ledmx = lib.getExe ledmxPackage;
in
{
  home.packages = [ ledmxPackage ];

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
