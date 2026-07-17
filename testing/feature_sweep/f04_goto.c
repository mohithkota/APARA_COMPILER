long long results[1];
int main(){ int i=0,s=0; loop: if(i<5){s+=i;i++;goto loop;} results[0]=s; return 1; } /*10*/
