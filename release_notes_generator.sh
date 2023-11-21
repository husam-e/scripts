#!/bin/bash

read -r -d '' USAGE << EOM
Usage: release_notes_helper.sh [OPTIONS] <START_VERSION> [END_VERSION]

    example 1 (from a release tag to main, for an upcoming tag):     ./release_notes_helper.sh v1.8.0 origin/main
    example 2 (from a certain start revision until the latest HEAD): ./release_notes_helper.sh v1.8.0
    example 3 (between two commits/tags):                            ./release_notes_helper.sh v1.8.0 v1.9.0

START_VERSION: required
END_VERSION: optional, default: HEAD
EOM

while getopts ":fh" opt; do
    case ${opt} in
        \? ) echo "${USAGE}" && exit -2
            ;;
    esac
done

START_VERSION=${@:$OPTIND:1}
if [ -z "${START_VERSION}" ]; then
  echo "${USAGE}"
  exit -1
fi

END_VERSION=${@:$OPTIND+1:1}
if [ -z "${END_VERSION}" ]; then
  END_VERSION="HEAD"
fi

echo "Running a git fetch to update local repositories..."
git fetch
echo "Completed git fetch"
echo ""
echo "Generating release notes from ${START_VERSION} to ${END_VERSION}"
echo "Copy the lines below into the release notes."
echo "-----"
git log ${START_VERSION}...${END_VERSION} --pretty=format:"- [(%h)](${CI_PROJECT_URL}/commit/%H) %s" --reverse |
    sort -k 3,3 |
    sed -e $'s/*/\\n  */g' |
    sed -e $'s/feat:/:trophy: *feat:*/g' |
    sed -e $'s/feat /:trophy: *feat:* /g' |
    sed -e $'s/feat(/:trophy: *feat*(/g' |
    sed -e $'s/feature(/:trophy: *feat*(/g' |
    sed -e $'s/fix:/:white_check_mark: *fix:*/g' |
    sed -e $'s/fix(/:white_check_mark: *fix*(/g' |
    sed -e $'s/) fix /) :white_check_mark: *fix:* /g' |
    sed -e $'s/chore:/:nut_and_bolt: *chore:*/g' |
    sed -e $'s/chore(/:nut_and_bolt: *chore*(/g' |
    sed -e $'s/refactor /:recycle: *refactor:* /g' |
    sed -e $'s/refactor:/:recycle: *refactor:*/g' |
    sed -e $'s/refactor(/:recycle: *refactor*(/g' |
    sed -e $'s/revert /:leftwards_arrow_with_hook: *revert:* /g' |
    sed -e $'s/revert:/:leftwards_arrow_with_hook: *revert:*/g' |
    sed -e $'s/revert(/:leftwards_arrow_with_hook: *revert*(/g' |
    sed -e $'s/test:/:mag: *test:*/g' |
    sed -e $'s/test(/:mag: *test*(/g' |
    sed -e $'s/docs:/:notebook_with_decorative_cover: *docs:*/g' |
    sed -e $'s/docs(/:notebook_with_decorative_cover: *docs*(/g' |
    sed -e $'s/perf:/:clock1: *perf:*/g' |
    sed -e $'s/perf(/:clock1: *perf*(/g' |
    sed -e $'s/style:/:art: *style:*/g' |
    sed -e $'s/style(/:art: *style*(/g' |
    sed -e $'s/build(/:twisted_rightwards_arrows: *build*(/g' |
    sed -e $'s/build:/:twisted_rightwards_arrows: *build:*/g' |
    sed -e $'s/ci(/:arrows_clockwise: *ci*(/g' |
    sed -e $'s/ci:/:arrows_clockwise: *ci:*/g'
echo "-----"

# TODO: remove blanks when Issue does not exist
tickets=($(git log ${START_VERSION}...${END_VERSION} --pretty=format:'%s' --reverse | grep -oe '#[0-9]\+'))
unique=($(echo "${tickets[@]}" | tr ' ' '\n' | sort -u | tr '\n' ' '))
echo "Commit range includes ${#unique[@]} Issues"
list=$(IFS=, ; echo "${unique[*]}")
# If JIRAPROJECT is set, output JIRA query
[[ ! -z "$JIRAPROJECT" ]] && echo "project=${JIRAPROJECT} and id in (${list})"
