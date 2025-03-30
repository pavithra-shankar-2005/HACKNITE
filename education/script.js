const video = document.getElementById("video");

navigator.mediaDevices.getUserMedia({ video: true })
    .then(stream => { video.srcObject = stream; })
    .catch(err => console.error(err));

function joinClass() {
    alert("Joining online class...");
    window.location.href = "/teacher_dashboard";
}

function startClass() {
    const link = "https://quantum-nxt-class/" + Math.random().toString(36).substr(2, 9);
    document.getElementById("class-link").innerText = link;
}