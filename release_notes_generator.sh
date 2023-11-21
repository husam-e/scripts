#!/bin/bash

read -r -d '' USAGE << EOM
Usage: release_notes_helper.sh [-j JIRAKEY] [-s SCOPES] <START_VERSION> [END_VERSION]

    example 1 (from a release tag to main, for an upcoming tag):      ./release_notes_helper.sh v1.1.0 origin/main
    example 2 (from a certain start revision until the latest HEAD):  ./release_notes_helper.sh v1.1.0
    example 3 (between two commits/tags):                             ./release_notes_helper.sh v1.1.0 v1.2.0
    example 4 (from a release tag to main, with JIRA key):            ./release_notes_helper.sh -j MYPROJ v1.1.0 origin/main
    example 5 (from a release tag to main, with JIRA key and scopes): ./release_notes_helper.sh -j MYPROJ -s "(ui)\|(backend)" v1.1.0 origin/main

"-j": optional, the JIRA Project Key e.g. "MYPROJ".
"-s": optinal, the scopes to filter commits for - anything that regular `grep` can understand and find in the commit message. Default: ".*"
START_VERSION: required
END_VERSION: optional, default: HEAD
EOM

while getopts ":j:s:e:fh" opt; do
    case ${opt} in
        j) JIRAKEY="${OPTARG}"
            ;;
        s) SCOPES="${OPTARG}"
            ;;
        h) echo "${USAGE}" && exit 0
            ;;
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

if [ -z "${JIRAKEY}" ]; then
  ISSUEPREFIX="#" # use default Github/GitLab issue prefix if no JIRA project specified
else
  ISSUEPREFIX="${JIRAKEY}-"
fi

if [ -z "${SCOPES}" ]; then
  SCOPES=".*" # default to everything (a no-op)
fi

echo "Running a git fetch to update local repositories..."
git fetch
echo "Completed git fetch"
echo ""
echo "Generating release notes from ${START_VERSION} to ${END_VERSION}"
echo "Copy the lines below into the release notes."
echo "-----"
git log ${START_VERSION}...${END_VERSION} --pretty=format:"- [(%h)](${CI_PROJECT_URL}/commit/%H) %s" --reverse |
    grep "${SCOPES}" |
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
issues=($(git log ${START_VERSION}...${END_VERSION} --pretty=format:'%s' --reverse | grep -oe "${ISSUEPREFIX}[0-9]\+"))
unique=($(echo "${issues[@]}" | tr ' ' '\n' | sort -u | tr '\n' ' '))
echo "Commit range includes ${#unique[@]} Issues"
list=$(IFS=, ; echo "${unique[*]}")
# If JIRAKEY is set, output JIRA query
[[ ! -z "$JIRAKEY" ]] && echo "project=${JIRAKEY} and id in (${list})"
