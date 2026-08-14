# ~/homelab-infra/shared/dotfiles/fish/functions/sysup.fish
function sysup --description "Full system update + AUR rebuild check"
    paru -Syu $argv
    echo -e "\n--- AUR packages needing rebuild ---"
    checkrebuild
end