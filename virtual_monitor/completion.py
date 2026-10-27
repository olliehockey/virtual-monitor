"""Shell completion scripts for `vm` (device ids are completed live)."""

COMMANDS = "list status enable disable pair unpair presets add edit remove web logs doctor autostart uninstall undo"
DEVICE_COMMANDS = "status enable on disable off pair unpair edit remove rm web logs autostart"

BASH = r"""# bash completion for vm (virtual-monitor)
_vm() {
    local cur prev words cword
    _init_completion 2>/dev/null || { cur="${COMP_WORDS[COMP_CWORD]}"; prev="${COMP_WORDS[COMP_CWORD-1]}"; words=("${COMP_WORDS[@]}"); cword=$COMP_CWORD; }
    local cmds="__COMMANDS__"
    local cmd="" i
    for ((i=1; i<cword; i++)); do
        [[ ${words[i]} != -* ]] && { cmd=${words[i]}; break; }
    done
    if [[ -z $cmd ]]; then
        COMPREPLY=($(compgen -W "$cmds --help --version" -- "$cur")); return
    fi
    case $prev in
        --position) COMPREPLY=($(compgen -W "right left above below" -- "$cur")); return;;
        --refresh|--scale|--port|--lines|-n) return;;
    esac
    if [[ $cur == -* ]]; then
        local opts
        opts=$(vm "$cmd" --help 2>/dev/null | grep -oE '(^|[ ,])--?[a-z][a-z-]*' | tr -d ' ,' | sort -u)
        COMPREPLY=($(compgen -W "$opts" -- "$cur")); return
    fi
    case $cmd in
        __DEVICE_COMMANDS_CASE__)
            local n=0
            for ((i=i+1; i<cword; i++)); do [[ ${words[i]} != -* ]] && ((n++)); done
            if [[ ($cmd == add || $cmd == edit) && $n -ge 1 ]]; then
                COMPREPLY=($(compgen -W "$(vm _complete presets 2>/dev/null)" -- "$cur"))
            elif [[ $cmd == autostart && $n -ge 1 ]]; then
                COMPREPLY=($(compgen -W "on off" -- "$cur"))
            elif [[ $cmd == enable || $cmd == disable || $n -eq 0 ]]; then
                COMPREPLY=($(compgen -W "$(vm _complete devices 2>/dev/null)" -- "$cur"))
            fi;;
        completion) COMPREPLY=($(compgen -W "bash zsh fish" -- "$cur"));;
    esac
}
complete -F _vm vm
"""

ZSH = r"""#compdef vm
# zsh completion for vm (uses the bash completion through bashcompinit)
autoload -Uz bashcompinit && bashcompinit
""" + BASH

FISH = r"""# fish completion for vm (virtual-monitor)
function __vm_devices; vm _complete devices 2>/dev/null; end
set -l cmds __COMMANDS__
complete -c vm -f
complete -c vm -n "not __fish_seen_subcommand_from $cmds" -a "$cmds"
complete -c vm -n "__fish_seen_subcommand_from __DEVICE_COMMANDS__" -a "(__vm_devices)"
complete -c vm -n "__fish_seen_subcommand_from autostart" -a "on off"
complete -c vm -n "__fish_seen_subcommand_from completion" -a "bash zsh fish"
complete -c vm -l position -x -a "right left above below"
complete -c vm -l json -d "machine-readable output"
complete -c vm -l help -d "show help"
"""


def _render(text: str) -> str:
    from .config import PRESETS
    return (text.replace("__PRESETS__", " ".join(PRESETS)).replace("__COMMANDS__", COMMANDS)
                .replace("__DEVICE_COMMANDS_CASE__", DEVICE_COMMANDS.replace(" ", "|"))
                .replace("__DEVICE_COMMANDS__", DEVICE_COMMANDS)).rstrip("\n")


SCRIPTS = {"bash": _render(BASH), "zsh": _render(ZSH), "fish": _render(FISH)}
