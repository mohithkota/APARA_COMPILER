long long results[2];
int main(){ int x=42; int *p=&x; results[0]=*p; *p=99; results[1]=x; return 1; }
