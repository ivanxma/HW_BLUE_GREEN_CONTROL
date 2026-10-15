'use strict';
const dialog = document.querySelector('#auth-dialog');
if (dialog) {
  dialog.close();
  dialog.showModal();
  dialog.addEventListener('cancel', event => event.preventDefault());
}
