class FakeAuth:
    """Test-only authentication double; production always connects to local MySQL."""
    def __init__(self, store, password='test-private-password', first_login=False):
        self.store=store
        self.password=password
        self.store.put('must-change', first_login)
        self.closed=[]

    def authenticate(self, username, password):
        return username=='admin' and password==self.password

    def must_change(self, username):return self.store.get('must-change')
    def healthy(self,*args):return True
    def close(self,sid):self.closed.append(sid)

    def change_password(self,username,old,new):
        if len(new)<12 or new in {old,'ChangeMe123!'}:
            raise ValueError('Choose a different password of at least 12 characters')
        if old != self.password:raise ValueError('Invalid current password')
        self.password=new
        self.store.put('must-change',False)
