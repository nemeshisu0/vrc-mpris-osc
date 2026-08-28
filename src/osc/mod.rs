//! Minimal OSC-over-UDP client targeting VRChat's Chatbox endpoint.
//!
//! VRChat listens on `127.0.0.1:9000` (by default) and expects an OSC message
//! at `/chatbox/input` with three arguments: the text (string), whether to
//! send immediately (bool), and whether to trigger a notification sound (bool).

use anyhow::{Context, Result};
use rosc::{encoder, OscMessage, OscPacket, OscType};
use std::net::SocketAddr;
use tokio::net::UdpSocket;

pub struct OscClient {
    socket: UdpSocket,
    target: SocketAddr,
    address_path: String,
}

impl OscClient {
    /// Binds an ephemeral local UDP socket and prepares to send to `host:port`.
    pub async fn connect(host: &str, port: u16, address_path: &str) -> Result<Self> {
        let target: SocketAddr = format!("{host}:{port}")
            .parse()
            .with_context(|| format!("invalid OSC target address {host}:{port}"))?;

        // Bind to any local ephemeral port on the matching family (v4 here,
        // since VRChat's OSC endpoint is always 127.0.0.1 in practice).
        let bind_addr = if target.is_ipv4() { "0.0.0.0:0" } else { "[::]:0" };
        let socket = UdpSocket::bind(bind_addr)
            .await
            .context("failed to bind local UDP socket for OSC output")?;

        Ok(Self {
            socket,
            target,
            address_path: address_path.to_string(),
        })
    }

    /// Sends a chatbox update. `send_immediately` bypasses the typing
    /// indicator delay, `notification` toggles the little popup/sound.
    pub async fn send_chatbox(
        &self,
        text: &str,
        send_immediately: bool,
        notification: bool,
    ) -> Result<()> {
        let packet = build_chatbox_packet(&self.address_path, text, send_immediately, notification)?;
        self.socket
            .send_to(&packet, self.target)
            .await
            .context("failed to send OSC UDP packet")?;
        Ok(())
    }
}

/// Builds the raw OSC bytes for a `/chatbox/input` message. Extracted as a
/// free function so it can be unit-tested without a real socket.
pub fn build_chatbox_packet(
    address_path: &str,
    text: &str,
    send_immediately: bool,
    notification: bool,
) -> Result<Vec<u8>> {
    let msg = OscMessage {
        addr: address_path.to_string(),
        args: vec![
            OscType::String(text.to_string()),
            OscType::Bool(send_immediately),
            OscType::Bool(notification),
        ],
    };
    let bytes = encoder::encode(&OscPacket::Message(msg)).context("failed to encode OSC packet")?;
    Ok(bytes)
}

#[cfg(test)]
mod tests {
    use super::*;
    use rosc::{decoder, OscPacket};

    #[test]
    fn encodes_and_decodes_roundtrip() {
        let bytes = build_chatbox_packet("/chatbox/input", "🎵 Queen - Bohemian Rhapsody", true, false)
            .unwrap();
        let (_, packet) = decoder::decode_udp(&bytes).unwrap();
        match packet {
            OscPacket::Message(msg) => {
                assert_eq!(msg.addr, "/chatbox/input");
                assert_eq!(msg.args.len(), 3);
                assert_eq!(msg.args[0], OscType::String("🎵 Queen - Bohemian Rhapsody".to_string()));
                assert_eq!(msg.args[1], OscType::Bool(true));
                assert_eq!(msg.args[2], OscType::Bool(false));
            }
            _ => panic!("expected an OscPacket::Message"),
        }
    }

    #[tokio::test]
    async fn client_can_send_to_loopback_listener() {
        // Spin up a fake OSC receiver on an ephemeral port and make sure the
        // client's packet actually arrives and decodes correctly.
        let listener = UdpSocket::bind("127.0.0.1:0").await.unwrap();
        let listener_addr = listener.local_addr().unwrap();

        let client = OscClient::connect("127.0.0.1", listener_addr.port(), "/chatbox/input")
            .await
            .unwrap();
        client.send_chatbox("Test Track", true, false).await.unwrap();

        let mut buf = [0u8; 1024];
        let (len, _) = listener.recv_from(&mut buf).await.unwrap();
        let (_, packet) = decoder::decode_udp(&buf[..len]).unwrap();
        match packet {
            OscPacket::Message(msg) => {
                assert_eq!(msg.addr, "/chatbox/input");
                assert_eq!(msg.args[0], OscType::String("Test Track".to_string()));
            }
            _ => panic!("expected an OscPacket::Message"),
        }
    }
}
