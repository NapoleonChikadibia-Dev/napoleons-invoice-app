// Premium login animation
const loginContent = document.querySelector(".login_content");
const loginImage = document.querySelector(".login_img");
const loginTitle = document.querySelector(".login_title");
const formItems = document.querySelectorAll(".login_form > *");

const reducedMotion = window.matchMedia(
  "(prefers-reduced-motion: reduce)"
).matches;

if (loginContent && !reducedMotion && window.gsap) {
  const tl = gsap.timeline();

  tl.fromTo(
    loginContent,
    {
      y: 28,
      scale: 0.97,
      opacity: 0
    },
    {
      y: 0,
      scale: 1,
      opacity: 1,
      duration: 0.8,
      ease: "power3.out"
    }
  );

  if (loginTitle) {
    tl.from(
      loginTitle,
      {
        y: 12,
        opacity: 0,
        duration: 0.45,
        ease: "power2.out"
      },
      "-=0.35"
    );
  }

  if (formItems.length) {
    tl.from(
      formItems,
      {
        y: 12,
        opacity: 0,
        duration: 0.45,
        stagger: 0.08,
        ease: "power2.out"
      },
      "-=0.2"
    );
  }

  if (loginImage) {
    gsap.fromTo(
      loginImage,
      {
        opacity: 0,
        x: 24
      },
      {
        opacity: 1,
        x: 0,
        duration: 0.8,
        delay: 0.2,
        ease: "power3.out"
      }
    );
  }
}

