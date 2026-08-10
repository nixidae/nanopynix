# The pieces of a git reference of `uv.lock`.
#
# **uv packs the whole reference into one string**, and that string is a URL, a
# query and a fragment:
#
#   https://github.com/pypa/hatch.git?subdirectory=backend&rev=hatchling-v1.27.0#cbf6598e5cbce3ba9097023c5bf783001ebbcbcb
#
# **`locked_git_url` of uv writes it, and that function is the authority here.**
# It is in `crates/uv-resolver/src/lock/mod.rs`, and `GitSource::from_url` of
# the same file reads the string back. Between them they define five keys and
# one fragment, and this file answers each one:
#
#   subdirectory  where the project lives inside the repository. Taken.
#   path          a **file** inside the repository, which is a wheel or an
#                 archive rather than a project. Refused below.
#   lfs           the checkout needs Git LFS. Refused below.
#   tag           what the user asked for. Dropped: a tag moves.
#   branch        the same. Dropped.
#   rev           the same. Dropped.
#   #<fragment>   the commit that uv resolved, always 40 hexadecimal
#                 characters. This is what the fetch names.
#
# uv also removes the credentials from the URL before it writes the string, so
# no reference of a lock file carries one.
#
# **This file is separate from `./uv.nix` so that a test can call it.**
# `tests/test_git_and_url_sources.py` runs it over the reference of each fixture
# lock file, and over the shapes that no fixture holds. `./uv.nix` returns a
# derivation that needs a network fetch, so no test can read the parse out of
# that.
#
# It takes no `lib`, so the test needs no nixpkgs to evaluate it.
#
# `name` names the package, for the message of a failure.
name: spec:

let
  # `builtins.split` puts a list between each pair of parts, and that list
  # holds the capture groups of the pattern. This pattern captures nothing, so
  # every string of the result is a part.
  parts = pattern: text: builtins.filter builtins.isString (builtins.split pattern text);

  atFragment = parts "#" spec;
  atQuery = parts "[?]" (builtins.head atFragment);

  # A reference with no query gives one part, and the query is then empty.
  queryText = if builtins.length atQuery > 1 then builtins.elemAt atQuery 1 else "";

  pair =
    text:
    let
      sides = parts "=" text;
    in
    {
      name = builtins.head sides;
      # A value may hold `=`, so only the first one separates.
      value = builtins.concatStringsSep "=" (builtins.tail sides);
    };

  query = builtins.listToAttrs (map pair (builtins.filter (text: text != "") (parts "&" queryText)));
in

if builtins.length atFragment != 2 then
  throw (
    "uv.nix: the git source of '${name}' is ${builtins.toJSON spec}, and it names no commit. "
    + "uv writes the commit after a '#', and this reader fetches that commit."
  )
# **A `path` key means that the package is a file of the repository, and not a
# directory of it.** `from_git_path_built_dist` and `from_git_path_source_dist`
# of uv write it, and each one carries a wheel or an archive that lives in the
# checkout. `graph.nix` would have to unpack that file rather than run a build
# over the tree, and no node does that yet. A silent build of the root of the
# repository would install the wrong project.
else if query ? path then
  throw (
    "uv.nix: the git source of '${name}' names 'path=${query.path}', so the package is a file "
    + "of that repository and not a directory of it. This reader builds a tree, and a node that "
    + "installs such a file is still to do."
  )
# **Git LFS keeps the content outside the repository, and `builtins.fetchGit`
# does not fetch it.** A checkout would then hold a pointer file of a hundred
# bytes where each large file belongs, and the build would read that pointer as
# if it were the file.
else if (query.lfs or "false") == "true" then
  throw (
    "uv.nix: the git source of '${name}' names 'lfs=true', and builtins.fetchGit fetches no "
    + "Git LFS object. The checkout would hold a pointer file in place of each large file."
  )
else
  {
    url = builtins.head atQuery;
    rev = builtins.elemAt atFragment 1;
    subdirectory = query.subdirectory or "";
  }
