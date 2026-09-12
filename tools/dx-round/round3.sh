#!/bin/zsh
# Dx round, part 3: D7/D8 on the fix-branch platform (codex/stage2-dx-round-fixes-20260911).
#
# Run only after: the fix-branch images are deployed with the Coroot variables
# kept, the three harnesses are requalified with the substitution profile and
# republished (code_execution=platform_sandbox), and one full
# qualification_probe run has written the D7 samples and both D8 canaries.
# Before every D7 run the hook refreshes the D7 samples for the live cart Pod.
# IMAGE must name the deployed image, e.g. IMAGE="<sha>+coroot".
HERE="${0:A:h}"
export IMAGE="${IMAGE:?set IMAGE to the deployed image label, e.g. <sha>+coroot}"
export PRE_RUN_HOOK="$HERE/refresh_d7.sh"
zsh "$HERE/chain_dx.sh" D7-A D7-B D8-A D8-B
