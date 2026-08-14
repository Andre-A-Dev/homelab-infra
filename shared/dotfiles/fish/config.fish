source /usr/share/cachyos-fish-config/cachyos-config.fish

# overwrite greeting
# potentially disabling fastfetch
#function fish_greeting
#    # smth smth
#end
alias lumux="source ~/.venv/lumux/bin/activate.fish && python -m lumux"

# Added by LM Studio CLI (lms)
set -gx PATH $PATH /home/youruser/.lmstudio/bin
# End of LM Studio CLI section

