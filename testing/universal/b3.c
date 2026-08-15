int flag[12]; long long results[3];
int main(){ int i,j; for(i=0;i<12;i++) flag[i]=1;
  for(i=2;i<12;i++) for(j=i*2;j<12;j+=i) flag[j]=0;
  results[0]=flag[4]; results[1]=flag[6]; results[2]=flag[5]; return 1; }
