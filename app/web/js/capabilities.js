export function hasWorkspaceAccess(identity) {
  return ["local", "authenticated"].includes(identity?.status);
}

export function hasRegisteredAccount(identity) {
  return identity?.status === "authenticated";
}
