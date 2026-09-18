# BlueBubbles LAN certificate

`bluebubbles-lan-root-ca.pem` is the public certificate for the private LAN
certificate authority. It is bundled with the Windows client after the server
is provisioned, so the client can verify the server without asking each Windows
user to install a root certificate manually.

Never place the CA private key or the server private key in this directory or
in Git. The provisioning script keeps those keys on the server under
`/etc/bluebubbles`.
