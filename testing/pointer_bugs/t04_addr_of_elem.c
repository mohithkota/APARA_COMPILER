int arr[4]={10,20,30,40}; long long results[2];
int main(){ int *p=&arr[1]; results[0]=*p; results[1]=p[1]; return 1; }
